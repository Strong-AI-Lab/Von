"""Tests for the opt-in LLM response cache (JVNAUTOSCI-2506)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services import llm_response_cache_service as cache


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.delenv(cache.LLM_RESPONSE_CACHE_ENV, raising=False)
    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)
    # Keep unit tests away from any real Mongo.
    monkeypatch.setattr(cache, "_persistent_collection", lambda: None)
    cache.clear_llm_response_cache()
    yield
    cache.clear_llm_response_cache()


def _key(**overrides: Any) -> str:
    params = {
        "provider": "ollama",
        "host": "http://127.0.0.1:11434",
        "model": "qwen3:8b",
        "messages": [{"role": "user", "content": "hello"}],
        "options": {"temperature": 0},
    }
    params.update(overrides)
    return cache.build_llm_response_cache_key(**params)


def test_disabled_by_default_outside_agent_test(monkeypatch):
    assert cache.is_llm_response_cache_enabled() is False
    cache.store_llm_response(
        _key(), provider="ollama", host="h", model="m", response_text="x"
    )
    assert cache.get_cached_llm_response(_key()) is None
    assert cache.get_llm_response_cache_stats()["stores"] == 0


def test_enabled_on_agent_test_instance_unless_explicitly_off(monkeypatch):
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    assert cache.is_llm_response_cache_enabled() is True
    monkeypatch.setenv(cache.LLM_RESPONSE_CACHE_ENV, "off")
    assert cache.is_llm_response_cache_enabled() is False


def test_round_trip_and_key_sensitivity(monkeypatch):
    monkeypatch.setenv(cache.LLM_RESPONSE_CACHE_ENV, "replay")
    key = _key()
    assert cache.get_cached_llm_response(key) is None

    cache.store_llm_response(
        key,
        provider="ollama",
        host="http://127.0.0.1:11434",
        model="qwen3:8b",
        response_text="cached answer",
        duration_ms=1234.5,
    )
    entry = cache.get_cached_llm_response(key)
    assert entry is not None
    assert entry["response_text"] == "cached answer"
    assert entry["original_duration_ms"] == pytest.approx(1234.5)

    # Any change to model, messages, or options misses.
    assert cache.get_cached_llm_response(_key(model="qwen3:14b")) is None
    assert (
        cache.get_cached_llm_response(
            _key(messages=[{"role": "user", "content": "hello!"}])
        )
        is None
    )
    assert (
        cache.get_cached_llm_response(_key(options={"temperature": 0.7})) is None
    )

    stats = cache.get_llm_response_cache_stats()
    assert stats["hits"] == 1
    assert stats["stores"] == 1
    assert stats["misses"] >= 3


def test_lru_bound(monkeypatch):
    monkeypatch.setenv(cache.LLM_RESPONSE_CACHE_ENV, "1")
    monkeypatch.setenv("VON_LLM_RESPONSE_CACHE_MAX_ENTRIES", "8")
    keys = []
    for index in range(20):
        key = _key(messages=[{"role": "user", "content": f"prompt {index}"}])
        keys.append(key)
        cache.store_llm_response(
            key, provider="ollama", host="h", model="m",
            response_text=f"answer {index}",
        )
    stats = cache.get_llm_response_cache_stats()
    assert stats["entries_in_memory"] <= 8
    assert cache.get_cached_llm_response(keys[-1]) is not None
    assert cache.get_cached_llm_response(keys[0]) is None


def test_empty_responses_are_not_cached(monkeypatch):
    monkeypatch.setenv(cache.LLM_RESPONSE_CACHE_ENV, "1")
    cache.store_llm_response(
        _key(), provider="ollama", host="h", model="m", response_text="   "
    )
    assert cache.get_llm_response_cache_stats()["stores"] == 0


def test_prompt_key_groups_models_for_cached_ab(monkeypatch):
    """Same prompt under different models shares a prompt_key, enabling
    cached A/B comparison with per-entry original timings."""

    monkeypatch.setenv(cache.LLM_RESPONSE_CACHE_ENV, "replay")
    messages = [{"role": "user", "content": "summarise the meeting"}]
    options = {"temperature": 0}
    prompt_key = cache.build_llm_prompt_key(messages=messages, options=options)

    for model, answer, ms in (
        ("qwen3:8b", "short summary", 900.0),
        ("qwen3:14b", "better summary", 2100.0),
    ):
        key = cache.build_llm_response_cache_key(
            provider="ollama", host="h", model=model,
            messages=messages, options=options,
        )
        cache.store_llm_response(
            key, provider="ollama", host="h", model=model,
            response_text=answer, duration_ms=ms, prompt_key=prompt_key,
        )

    entries = cache.get_cached_responses_by_prompt(prompt_key)
    assert [e["model"] for e in entries] == ["qwen3:14b", "qwen3:8b"]
    by_model = {e["model"]: e for e in entries}
    assert by_model["qwen3:8b"]["response_text"] == "short summary"
    assert by_model["qwen3:14b"]["original_duration_ms"] == pytest.approx(2100.0)


def test_ollama_generate_uses_cache(monkeypatch):
    """End-to-end through OllamaClient.generate: second call never hits chat."""

    monkeypatch.setenv(cache.LLM_RESPONSE_CACHE_ENV, "replay")
    from src.backend.languagemodels.llm_interface import OllamaClient

    client = OllamaClient.__new__(OllamaClient)
    client.host = "http://127.0.0.1:11434"
    client.default_model = "qwen3:8b"
    chat_calls = {"count": 0}

    class _FakeOllama:
        def chat(self, *, model, messages, options=None):
            chat_calls["count"] += 1
            return {"message": {"content": f"generated by {model}"}}

    client.client = _FakeOllama()

    with patch(
        "src.backend.languagemodels.local_model_preflight.enforce_local_model_preflight",
        lambda *args, **kwargs: None,
    ):
        first = client.generate("what is 2+2?", context=None, model="qwen3:8b")
        second = client.generate("what is 2+2?", context=None, model="qwen3:8b")

    assert first == "generated by qwen3:8b"
    assert second == first
    assert chat_calls["count"] == 1
    stats = cache.get_llm_response_cache_stats()
    assert stats["hits"] == 1
    assert stats["stores"] == 1


def test_ollama_generate_maps_thinking_control_to_chat_transport(monkeypatch):
    from src.backend.languagemodels.llm_interface import OllamaClient

    client = OllamaClient.__new__(OllamaClient)
    client.host = "http://127.0.0.1:11434"
    client.default_model = "qwen3:8b"
    captured: dict[str, Any] = {}

    class _FakeOllama:
        def chat(self, **kwargs: Any) -> dict[str, dict[str, str]]:
            captured.update(kwargs)
            return {"message": {"content": '["Continue", "Stop"]'}}

    client.client = _FakeOllama()

    with patch(
        "src.backend.languagemodels.local_model_preflight.enforce_local_model_preflight",
        lambda *args, **kwargs: None,
    ):
        response = client.generate(
            "Return two options.",
            model="qwen3:8b",
            llm_params={"think": False, "num_predict": 128},
        )

    assert response == '["Continue", "Stop"]'
    assert captured["think"] is False
    assert captured["options"] == {"num_predict": 128}
