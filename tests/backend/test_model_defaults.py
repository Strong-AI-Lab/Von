"""Tests for centralised model defaults and 'default' token resolution."""

from __future__ import annotations

from typing import Any, Mapping, cast

import pytest


# ---------------------------------------------------------------------------
# model_defaults env-var override tests
# ---------------------------------------------------------------------------


class TestModelDefaults:
    """Verify that model_defaults respects environment variables."""

    def test_default_ollama_model_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("VON_DEFAULT_OLLAMA_MODEL", "llama3:8b")
        # Force re-import to pick up the env change
        import importlib

        import src.backend.languagemodels.model_defaults as mod

        importlib.reload(mod)
        assert mod.DEFAULT_OLLAMA_MODEL == "llama3:8b"

    def test_default_openai_model_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("VON_DEFAULT_OPENAI_MODEL", "gpt-5")
        import importlib

        import src.backend.languagemodels.model_defaults as mod

        importlib.reload(mod)
        assert mod.DEFAULT_OPENAI_MODEL == "gpt-5"

    def test_default_gemini_model_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("VON_DEFAULT_GEMINI_MODEL", "gemini-ultra")
        import importlib

        import src.backend.languagemodels.model_defaults as mod

        importlib.reload(mod)
        assert mod.DEFAULT_GEMINI_MODEL == "gemini-ultra"

    def test_hardcoded_fallbacks_when_env_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("VON_DEFAULT_OLLAMA_MODEL", raising=False)
        monkeypatch.delenv("VON_DEFAULT_OPENAI_MODEL", raising=False)
        monkeypatch.delenv("VON_DEFAULT_GEMINI_MODEL", raising=False)
        import importlib

        import src.backend.languagemodels.model_defaults as mod

        importlib.reload(mod)
        assert mod.DEFAULT_OLLAMA_MODEL == "gemma4:31b"
        assert mod.DEFAULT_OPENAI_MODEL == "gpt-5.5"
        assert mod.DEFAULT_GEMINI_MODEL == "gemini-3.7-flash"


# ---------------------------------------------------------------------------
# _parse_policy_model_candidate "default" token resolution
# ---------------------------------------------------------------------------


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):
        raise AssertionError("Gateway should not be invoked in this test")


class TestParsePolicyDefaultToken:
    """Verify that 'provider:default' resolves to configured default model."""

    def _get_orchestrator(self):
        from src.backend.integrations.internal_mcp.orchestrator import (
            InternalMCPChatOrchestrator,
        )

        return InternalMCPChatOrchestrator(gateway=cast(Any, _StubGateway()))

    def test_ollama_default_token(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("VON_DEFAULT_OLLAMA_MODEL", "test-ollama:7b")
        import importlib

        import src.backend.languagemodels.model_defaults as mod

        importlib.reload(mod)

        from src.backend.integrations.internal_mcp.orchestrator import (
            InternalMCPChatOrchestrator,
        )

        candidate = InternalMCPChatOrchestrator._parse_policy_model_candidate(
            "ollama:default"
        )
        assert candidate is not None
        assert candidate.provider == "ollama"
        assert candidate.model == "test-ollama:7b"
        assert candidate.source == "policy"

    def test_openai_default_token(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("VON_DEFAULT_OPENAI_MODEL", "gpt-99")
        import importlib

        import src.backend.languagemodels.model_defaults as mod

        importlib.reload(mod)

        from src.backend.integrations.internal_mcp.orchestrator import (
            InternalMCPChatOrchestrator,
        )

        candidate = InternalMCPChatOrchestrator._parse_policy_model_candidate(
            "openai:default"
        )
        assert candidate is not None
        assert candidate.provider == "openai"
        assert candidate.model == "gpt-99"

    def test_unknown_provider_default_returns_none(self):
        from src.backend.integrations.internal_mcp.orchestrator import (
            InternalMCPChatOrchestrator,
        )

        candidate = InternalMCPChatOrchestrator._parse_policy_model_candidate(
            "unknownprovider:default"
        )
        assert candidate is None

    def test_explicit_model_not_affected(self):
        from src.backend.integrations.internal_mcp.orchestrator import (
            InternalMCPChatOrchestrator,
        )

        candidate = InternalMCPChatOrchestrator._parse_policy_model_candidate(
            "ollama:gemma4:26b"
        )
        assert candidate is not None
        assert candidate.provider == "ollama"
        # "gemma4:26b" — not rewritten
        assert candidate.model is not None and "gemma4" in candidate.model


# ---------------------------------------------------------------------------
# _stage_model_candidates no-hardcoded-granite test
# ---------------------------------------------------------------------------


class TestStageModelCandidatesNoHardcodedGranite:
    """Ensure _stage_model_candidates does not produce granite3.3:2b candidates
    when the environment default is set to a different model."""

    def test_no_granite_in_candidates_when_env_overridden(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("VON_DEFAULT_OLLAMA_MODEL", "gemma4:26b")
        import importlib

        import src.backend.languagemodels.model_defaults as mod

        importlib.reload(mod)

        from src.backend.integrations.internal_mcp.orchestrator import (
            InternalMCPChatOrchestrator,
            _WorkflowModelPolicyState,
        )

        orch = InternalMCPChatOrchestrator(gateway=cast(Any, _StubGateway()))

        policy_state = _WorkflowModelPolicyState(
            enabled=False, policy=None, policy_id=None, predicate_id=None, errors=()
        )

        candidates = orch._stage_model_candidates(
            stage="planner",
            default_model="gemma4:26b",
            policy_state=policy_state,
        )

        model_names = [c.model for c in candidates if c.model]
        assert "granite3.3:2b" not in model_names
