from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.backend.languagemodels.structured_tool_calling import LLMClientConfig


class _RateLimited(RuntimeError):
    status_code = 429


class _Responses:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _openai_response(text: str = "backup answer") -> SimpleNamespace:
    return SimpleNamespace(
        model="gpt-5.6-luna",
        output_text=text,
        usage=SimpleNamespace(input_tokens=3, output_tokens=2, total_tokens=5),
    )


def test_openai_plain_generation_retries_once_with_same_model_and_backup_key(
    monkeypatch,
):
    from src.backend.languagemodels import llm_interface

    client = llm_interface.OpenAIClient(
        api_key="primary-secret",
        backup_api_key="backup-secret",
    )
    primary_responses = _Responses([_RateLimited("limited")])
    backup_responses = _Responses([_openai_response()])
    client.client = SimpleNamespace(responses=primary_responses)
    client._backup_client = SimpleNamespace(responses=backup_responses)
    monkeypatch.setattr(client, "_assert_model_execution_allowed", lambda **_: None)

    result = client.generate("Answer", model="gpt-5.6-luna")

    assert result == "backup answer"
    assert primary_responses.calls[0]["model"] == "gpt-5.6-luna"
    assert backup_responses.calls[0]["model"] == "gpt-5.6-luna"
    assert client.last_response_metadata["transport_metadata"] == {
        "provider": "openai",
        "requested_model": "gpt-5.6-luna",
        "effective_model": "gpt-5.6-luna",
        "effective_api_surface": "responses",
        "credential_source": "backup",
        "credential_failover_used": True,
        "primary_credential_failure_kind": "rate_limited",
    }
    assert "primary-secret" not in repr(client.last_response_metadata)
    assert "backup-secret" not in repr(client.last_response_metadata)


def test_openai_plain_generation_does_not_touch_backup_after_primary_success(
    monkeypatch,
):
    from src.backend.languagemodels import llm_interface

    client = llm_interface.OpenAIClient(
        api_key="primary-secret",
        backup_api_key="backup-secret",
    )
    primary_responses = _Responses([_openai_response("primary answer")])
    backup_responses = _Responses([_openai_response()])
    client.client = SimpleNamespace(responses=primary_responses)
    client._backup_client = SimpleNamespace(responses=backup_responses)
    monkeypatch.setattr(client, "_assert_model_execution_allowed", lambda **_: None)

    assert client.generate("Answer", model="gpt-5.6-luna") == "primary answer"
    assert len(primary_responses.calls) == 1
    assert backup_responses.calls == []
    assert (
        client.last_response_metadata["transport_metadata"]["credential_source"]
        == "primary"
    )
    assert (
        client.last_response_metadata["transport_metadata"]["credential_failover_used"]
        is False
    )


def test_openai_plain_generation_does_not_retry_unrelated_failure(monkeypatch):
    from src.backend.languagemodels import llm_interface

    client = llm_interface.OpenAIClient(
        api_key="primary-secret",
        backup_api_key="backup-secret",
    )
    primary_responses = _Responses([ValueError("invalid request")])
    backup_responses = _Responses([_openai_response()])
    client.client = SimpleNamespace(responses=primary_responses)
    client._backup_client = SimpleNamespace(responses=backup_responses)
    monkeypatch.setattr(client, "_assert_model_execution_allowed", lambda **_: None)

    with pytest.raises(RuntimeError, match="invalid request"):
        client.generate("Answer", model="gpt-5.6-luna")

    assert len(primary_responses.calls) == 1
    assert backup_responses.calls == []


class _Interactions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _gemini_response() -> SimpleNamespace:
    return SimpleNamespace(
        id="gemini-backup-response",
        model="gemini-3.7-flash",
        status="completed",
        output_text="gemini backup answer",
        steps=[],
        errors=[],
        usage=None,
    )


def test_gemini_plain_generation_retries_once_and_marks_backup(monkeypatch):
    from src.backend.languagemodels import llm_interface

    primary_interactions = _Interactions([_RateLimited("limited")])
    backup_interactions = _Interactions([_gemini_response()])
    primary_client = SimpleNamespace(interactions=primary_interactions)
    backup_client = SimpleNamespace(interactions=backup_interactions)
    fake_genai = SimpleNamespace(Client=lambda **_: primary_client)
    monkeypatch.setattr(llm_interface, "genai", fake_genai)
    monkeypatch.setattr(
        llm_interface, "assert_model_execution_allowed", lambda **_: None
    )

    client = llm_interface.GeminiClient(
        api_key="primary-secret",
        backup_api_key="backup-secret",
        default_model="gemini-3.7-flash",
    )
    client._backup_client = backup_client

    assert client.generate("Answer") == "gemini backup answer"
    assert primary_interactions.calls[0]["model"] == "gemini-3.7-flash"
    assert backup_interactions.calls[0]["model"] == "gemini-3.7-flash"
    assert (
        client.last_response_metadata["transport_metadata"]["credential_source"]
        == "backup"
    )
    assert (
        client.last_response_metadata["transport_metadata"]["credential_failover_used"]
        is True
    )
    assert (
        client.last_response_metadata["transport_metadata"][
            "primary_credential_failure_kind"
        ]
        == "rate_limited"
    )


def test_llm_client_config_repr_does_not_expose_credentials():
    config = LLMClientConfig(
        model="gpt-5.5",
        provider="openai",
        api_key="primary-secret",
        backup_api_key="backup-secret",
    )

    assert "primary-secret" not in repr(config)
    assert "backup-secret" not in repr(config)
