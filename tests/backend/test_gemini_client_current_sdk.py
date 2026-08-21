from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.version import Version


class _Config:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _Interactions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(dict(kwargs))
        return SimpleNamespace(
            id="interaction-text",
            model="gemini-3.7-flash-20260815",
            status="completed",
            output_text="A grounded answer.",
            steps=[],
            usage=SimpleNamespace(
                total_input_tokens=10,
                total_output_tokens=4,
                total_thought_tokens=3,
                total_tokens=17,
            ),
        )


def test_gemini_37_plain_generate_uses_stateless_interactions_and_system_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels import llm_interface
    from src.backend.services import model_parameter_service

    interactions = _Interactions()
    fake_client = SimpleNamespace(interactions=interactions)
    fake_genai = SimpleNamespace(
        Client=lambda **_kwargs: fake_client,
        types=SimpleNamespace(
            GenerateContentConfig=_Config,
            EmbedContentConfig=_Config,
        ),
    )
    monkeypatch.setattr(llm_interface, "genai", fake_genai)
    monkeypatch.setattr(
        llm_interface, "assert_model_execution_allowed", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        model_parameter_service,
        "_registry_parameter_policy",
        lambda **_kwargs: None,
    )

    client = llm_interface.GeminiClient(
        api_key="test-key",
        default_model="gemini-3.7-flash",
    )
    result = client.generate(
        "What follows?",
        context=[
            {"role": "system", "content": "Cite primary sources."},
            {"role": "user", "content": "Question one."},
            {"role": "assistant", "content": "Answer one."},
        ],
        llm_params={"reasoning_effort": "medium"},
    )

    assert result == "A grounded answer."
    assert interactions.requests == [
        {
            "model": "gemini-3.7-flash",
            "input": [
                {
                    "type": "user_input",
                    "content": [{"type": "text", "text": "Question one."}],
                },
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": "Answer one."}],
                },
                {
                    "type": "user_input",
                    "content": [{"type": "text", "text": "What follows?"}],
                },
            ],
            "store": False,
            "generation_config": {"thinking_level": "medium"},
            "system_instruction": "Cite primary sources.",
        }
    ]
    assert client.last_response_metadata["api_surface"] == "interactions"
    assert client.last_response_metadata["effective_model"] == (
        "gemini-3.7-flash-20260815"
    )
    assert client.last_response_metadata["usage"] == {
        "input_tokens": 10,
        "output_tokens": 7,
        "visible_output_tokens": 4,
        "total_tokens": 17,
        "thought_tokens": 3,
    }


def test_selected_gemini_failure_does_not_silently_substitute_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels import llm_interface

    monkeypatch.setattr(llm_interface, "initialize_clients", lambda **_kwargs: None)
    monkeypatch.setattr(
        llm_interface,
        "GeminiClient",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad Gemini config")),
    )
    monkeypatch.setattr(
        llm_interface,
        "_ensure_ollama_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("Ollama must not be consulted for Gemini failure")
        ),
    )

    with pytest.raises(RuntimeError, match="No cross-provider fallback"):
        llm_interface.get_llm_client(client_type="gemini")


def test_plain_interactions_rejects_failed_status_and_redacts_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels import llm_interface

    class _FailedInteractions:
        def create(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                id="failed-interaction",
                model="gemini-3.7-flash",
                status="failed",
                output_text="plausible but invalid",
                steps=[],
                usage=None,
                errors=[
                    SimpleNamespace(
                        model_dump=lambda **_kwargs: {
                            "code": "provider_failure",
                            "message": "api_key=secret-provider-value",
                        }
                    )
                ],
            )

    fake_client = SimpleNamespace(interactions=_FailedInteractions())
    monkeypatch.setattr(
        llm_interface,
        "genai",
        SimpleNamespace(Client=lambda **_kwargs: fake_client),
    )
    monkeypatch.setattr(
        llm_interface, "assert_model_execution_allowed", lambda **_kwargs: None
    )
    client = llm_interface.GeminiClient(
        api_key="test-key",
        default_model="gemini-3.7-flash",
    )

    with pytest.raises(RuntimeError, match="provider status failed") as exc_info:
        client.generate("Probe Gemini.")

    assert "secret-provider-value" not in str(exc_info.value)
    raw_errors = client.last_response_metadata["raw_response"]["errors"]
    assert "secret-provider-value" not in str(raw_errors)
    assert "[redacted]" in str(raw_errors)


def test_declared_google_genai_floor_matches_verified_locked_v2_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requirement = next(
        Requirement(value)
        for value in project["project"]["dependencies"]
        if value.startswith("google-genai")
    )
    lock = tomllib.loads((root / "pdm.lock").read_text(encoding="utf-8"))
    locked_version = Version(
        next(
            package["version"]
            for package in lock["package"]
            if package["name"] == "google-genai"
        )
    )

    assert Version("2.6.0") in requirement.specifier
    assert Version("2.5.999") not in requirement.specifier
    assert locked_version == Version("2.6.0")
    assert locked_version in requirement.specifier
