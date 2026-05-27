from __future__ import annotations

import pytest

import src.backend.services  # noqa: F401


def test_model_not_found_detection_from_response_error_shape() -> None:
    import src.backend.languagemodels.llm_interface as mod

    class _FakeResponseError(Exception):
        def __init__(self, message: str, status_code: int) -> None:
            super().__init__(message)
            self.status_code = status_code

    assert mod._is_ollama_model_not_found_error(
        _FakeResponseError("model 'gemma4:26b' not found", 404)
    )
    assert not mod._is_ollama_model_not_found_error(
        _FakeResponseError("upstream unavailable", 503)
    )


def test_get_embedding_retries_once_after_successful_auto_pull(monkeypatch) -> None:
    import src.backend.languagemodels.llm_interface as mod

    class _FakeResponseError(Exception):
        def __init__(self, message: str, status_code: int) -> None:
            super().__init__(message)
            self.status_code = status_code

    class _FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def embeddings(self, *, model: str, prompt: str):
            self.calls += 1
            if self.calls == 1:
                raise _FakeResponseError("model 'gemma4:26b' not found", 404)
            return {"embedding": [0.1, 0.2]}

    client = mod.OllamaClient.__new__(mod.OllamaClient)
    client.client = _FakeClient()
    client.default_model = "gemma4:26b"
    client.host = "http://127.0.0.1:11434"

    monkeypatch.setattr(
        client,
        "_attempt_model_auto_pull",
        lambda model_name: {
            "attempted": True,
            "succeeded": True,
            "failed": False,
            "model": model_name,
            "retry_outcome": "retried_after_pull",
        },
    )

    embedding = client.get_embedding("hello", model="gemma4:26b")

    assert embedding == [0.1, 0.2]
    assert client.client.calls == 2


def test_get_embedding_reports_auto_pull_failure(monkeypatch) -> None:
    import src.backend.languagemodels.llm_interface as mod

    class _FakeResponseError(Exception):
        def __init__(self, message: str, status_code: int) -> None:
            super().__init__(message)
            self.status_code = status_code

    class _FakeClient:
        def embeddings(self, *, model: str, prompt: str):
            raise _FakeResponseError("model 'gemma4:26b' not found", 404)

    client = mod.OllamaClient.__new__(mod.OllamaClient)
    client.client = _FakeClient()
    client.default_model = "gemma4:26b"
    client.host = "http://127.0.0.1:11434"

    monkeypatch.setattr(
        client,
        "_attempt_model_auto_pull",
        lambda model_name: {
            "attempted": True,
            "succeeded": False,
            "failed": True,
            "model": model_name,
            "retry_outcome": "pull_failed",
            "error": "download failed",
        },
    )

    with pytest.raises(RuntimeError) as exc_info:
        client.get_embedding("hello", model="gemma4:26b")

    message = str(exc_info.value)
    assert "Auto-pull outcome" in message
    assert "pull_failed" in message


def test_auto_pull_retry_budget_prevents_pull_storm(monkeypatch) -> None:
    import src.backend.languagemodels.llm_interface as mod

    client = mod.OllamaClient.__new__(mod.OllamaClient)
    client.client = object()
    client.default_model = "gemma4:26b"
    client.host = "http://127.0.0.1:11434"

    pull_calls = {"count": 0}

    monkeypatch.setattr(mod, "_OLLAMA_AUTO_PULL_ENABLED", True)
    monkeypatch.setattr(mod, "_OLLAMA_AUTO_PULL_RETRY_BUDGET", 1)
    monkeypatch.setattr(mod, "_OLLAMA_AUTO_PULL_COOLDOWN_SECONDS", 600.0)
    monkeypatch.setattr(mod, "_OLLAMA_AUTO_PULL_STATE", {})
    monkeypatch.setattr(client, "_is_model_available", lambda model_name: False)

    def _raise_pull(model_name: str) -> None:
        pull_calls["count"] += 1
        raise RuntimeError("pull failed")

    monkeypatch.setattr(client, "_pull_model", _raise_pull)

    first = client._attempt_model_auto_pull("gemma4:26b")
    second = client._attempt_model_auto_pull("gemma4:26b")

    assert first["attempted"] is True
    assert first["failed"] is True
    assert second["retry_outcome"] == "retry_budget_exhausted"
    assert pull_calls["count"] == 1
