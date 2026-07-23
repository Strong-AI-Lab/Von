"""Regression tests for the local-model preflight capacity check.

JVNAUTOSCI-2384.
"""

from __future__ import annotations

import warnings

import pytest

from src.backend.languagemodels import local_model_preflight as preflight
from src.backend.languagemodels.local_model_preflight import (
    HostCapacity,
    ModelFootprint,
    ModelTooLargeForHostError,
    build_user_message,
    detect_host_capacity,
    enforce_local_model_preflight,
    estimate_model_footprint,
    llm_stage,
    preflight_local_model_fits,
)

_GIB = 1024**3


class _FakeOllamaClient:
    """Minimal stand-in exposing the metadata methods the estimator uses."""

    def __init__(self, *, listing=None, ps=None, show=None, chat_response=None):
        self._listing = listing
        self._ps = ps
        self._show = show
        self._chat_response = chat_response or {"message": {"content": "ok"}}
        self.chat_calls = []

    def list(self):
        if self._listing is None:
            raise RuntimeError("no listing configured")
        return self._listing

    def ps(self):
        if self._ps is None:
            raise RuntimeError("no ps configured")
        return self._ps

    def show(self, model):
        if self._show is None:
            raise RuntimeError("no show configured")
        return self._show

    def chat(self, *, model, messages, options=None):
        self.chat_calls.append(model)
        return self._chat_response


@pytest.fixture(autouse=True)
def _clear_caches_and_enable(monkeypatch):
    preflight._capacity_cache = None
    preflight._footprint_cache.clear()
    monkeypatch.delenv("VON_LOCAL_MODEL_PREFLIGHT_ENABLED", raising=False)
    yield
    preflight._capacity_cache = None
    preflight._footprint_cache.clear()


# ---------------------------------------------------------------------------
# Footprint estimation
# ---------------------------------------------------------------------------
def test_footprint_from_ollama_listing_is_metadata_confidence():
    client = _FakeOllamaClient(
        listing={"models": [{"name": "gemma4:31b", "size": 40 * _GIB}]}
    )
    footprint = estimate_model_footprint(
        "gemma4:31b", ollama_client=client, use_cache=False
    )
    assert footprint.confidence == "metadata"
    assert footprint.source == "ollama.list"
    # On-disk size scaled by the runtime overhead factor (>= raw size).
    assert footprint.estimated_bytes is not None
    assert footprint.estimated_bytes >= 40 * _GIB


def test_footprint_prefers_resident_vram_size_from_ps():
    client = _FakeOllamaClient(
        ps={"models": [{"name": "gemma4:31b", "size_vram": 47 * _GIB}]},
        listing={"models": [{"name": "gemma4:31b", "size": 20 * _GIB}]},
    )
    footprint = estimate_model_footprint(
        "gemma4:31b", ollama_client=client, use_cache=False
    )
    assert footprint.source == "ollama.ps"
    assert footprint.estimated_bytes == 47 * _GIB


def test_footprint_heuristic_when_no_metadata():
    footprint = estimate_model_footprint(
        "gemma4:31b", ollama_client=None, use_cache=False
    )
    assert footprint.confidence == "heuristic"
    assert footprint.estimated_bytes is not None and footprint.estimated_bytes > 0


def test_footprint_unknown_when_no_param_token_and_no_metadata():
    footprint = estimate_model_footprint(
        "some-custom-model", ollama_client=None, use_cache=False
    )
    assert footprint.confidence == "unknown"
    assert footprint.estimated_bytes is None


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------
def test_verdict_too_large_carries_model_footprint_capacity_stage():
    capacity = HostCapacity(
        total_bytes=16 * _GIB, available_bytes=8 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:31b", 47 * _GIB, "metadata", "test")
    verdict = preflight_local_model_fits(
        "gemma4:31b",
        stage="plain_response",
        capacity=capacity,
        footprint=footprint,
    )
    assert verdict.fits is False
    assert verdict.checked is True
    assert verdict.model == "gemma4:31b"
    assert verdict.stage == "plain_response"
    assert verdict.footprint.estimated_bytes == 47 * _GIB
    assert verdict.capacity.total_bytes == 16 * _GIB
    assert verdict.reason == "model_estimated_too_large_for_host"


def test_verdict_fits_when_model_small_enough():
    capacity = HostCapacity(
        total_bytes=64 * _GIB, available_bytes=40 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:e4b", 10 * _GIB, "metadata", "test")
    verdict = preflight_local_model_fits(
        "gemma4:e4b", stage="plain_response", capacity=capacity, footprint=footprint
    )
    assert verdict.fits is True
    assert verdict.reason == "fits"


def test_verdict_does_not_block_on_unknown_footprint():
    capacity = HostCapacity(
        total_bytes=8 * _GIB, available_bytes=4 * _GIB, source="test"
    )
    footprint = ModelFootprint("mystery", None, "unknown", "test")
    verdict = preflight_local_model_fits(
        "mystery", capacity=capacity, footprint=footprint
    )
    assert verdict.fits is True
    assert verdict.reason == "insufficient_information"


def test_verdict_does_not_block_when_capacity_unknown():
    capacity = HostCapacity(
        total_bytes=None, available_bytes=None, source="unavailable"
    )
    footprint = ModelFootprint("gemma4:31b", 47 * _GIB, "metadata", "test")
    verdict = preflight_local_model_fits(
        "gemma4:31b", capacity=capacity, footprint=footprint
    )
    assert verdict.fits is True
    assert verdict.reason == "insufficient_information"


def test_heuristic_estimate_is_advisory_only_by_default():
    # The parameter-count heuristic is non-authoritative: even far above usable
    # capacity it must NOT hard-fail a load by default, only record the concern.
    capacity = HostCapacity(
        total_bytes=20 * _GIB, available_bytes=10 * _GIB, source="test"
    )
    big = ModelFootprint("x:70b", 60 * _GIB, "heuristic", "test")
    verdict = preflight_local_model_fits("x:70b", capacity=capacity, footprint=big)
    assert verdict.fits is True
    assert verdict.reason == "heuristic_estimate_exceeds_host_not_authoritative"
    assert verdict.details["estimate_exceeds_host"] is True
    assert verdict.details["heuristic_advisory_only"] is True


def test_heuristic_blocking_can_be_enabled_by_operator(monkeypatch):
    monkeypatch.setenv("VON_LOCAL_MODEL_PREFLIGHT_BLOCK_ON_HEURISTIC", "1")
    capacity = HostCapacity(
        total_bytes=20 * _GIB, available_bytes=10 * _GIB, source="test"
    )
    # usable = 18 GiB; heuristic margin 1.25 => threshold 22.5 GiB.
    # Only slightly above usable should still NOT block (margin protects it).
    near = ModelFootprint("x:20b", 20 * _GIB, "heuristic", "test")
    verdict_near = preflight_local_model_fits(
        "x:20b", capacity=capacity, footprint=near
    )
    assert verdict_near.fits is True

    # Far above capacity blocks once the operator opts in.
    big = ModelFootprint("x:70b", 60 * _GIB, "heuristic", "test")
    verdict_big = preflight_local_model_fits("x:70b", capacity=capacity, footprint=big)
    assert verdict_big.fits is False
    assert verdict_big.reason == "model_estimated_too_large_for_host"


def test_preflight_disabled_skips_check(monkeypatch):
    monkeypatch.setenv("VON_LOCAL_MODEL_PREFLIGHT_ENABLED", "false")
    capacity = HostCapacity(
        total_bytes=16 * _GIB, available_bytes=8 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:31b", 47 * _GIB, "metadata", "test")
    verdict = preflight_local_model_fits(
        "gemma4:31b", capacity=capacity, footprint=footprint
    )
    assert verdict.fits is True
    assert verdict.checked is False
    assert verdict.reason == "preflight_disabled"


# ---------------------------------------------------------------------------
# enforce_local_model_preflight + telemetry
# ---------------------------------------------------------------------------
def test_enforce_raises_typed_error_with_clean_message_and_telemetry(monkeypatch):
    capacity = HostCapacity(
        total_bytes=16 * _GIB, available_bytes=8 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:31b", 47 * _GIB, "metadata", "test")
    monkeypatch.setattr(preflight, "detect_host_capacity", lambda **_: capacity)
    monkeypatch.setattr(
        preflight, "estimate_model_footprint", lambda *a, **k: footprint
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ModelTooLargeForHostError) as exc_info:
            enforce_local_model_preflight("gemma4:31b", stage="plain_response")

    err = exc_info.value
    assert "gemma4:31b" in str(err)
    assert "too large" in str(err).lower()
    # Structured telemetry payload is attached to the exception verdict.
    telemetry = err.verdict.as_telemetry()
    assert telemetry["type"] == "local_model_preflight"
    assert telemetry["fits"] is False
    assert telemetry["model"] == "gemma4:31b"
    assert telemetry["stage"] == "plain_response"
    assert telemetry["footprint"]["estimated_bytes"] == 47 * _GIB
    assert telemetry["capacity"]["total_bytes"] == 16 * _GIB
    # A typed warning was emitted for telemetry/log capture.
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_enforce_returns_verdict_when_model_fits(monkeypatch):
    capacity = HostCapacity(
        total_bytes=64 * _GIB, available_bytes=40 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:e4b", 10 * _GIB, "metadata", "test")
    monkeypatch.setattr(preflight, "detect_host_capacity", lambda **_: capacity)
    monkeypatch.setattr(
        preflight, "estimate_model_footprint", lambda *a, **k: footprint
    )
    verdict = enforce_local_model_preflight("gemma4:e4b", stage="plain_response")
    assert verdict.fits is True


def test_stage_context_is_picked_up_by_verdict(monkeypatch):
    capacity = HostCapacity(
        total_bytes=16 * _GIB, available_bytes=8 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:31b", 47 * _GIB, "metadata", "test")
    monkeypatch.setattr(preflight, "detect_host_capacity", lambda **_: capacity)
    monkeypatch.setattr(
        preflight, "estimate_model_footprint", lambda *a, **k: footprint
    )
    with llm_stage("selector"):
        verdict = preflight_local_model_fits("gemma4:31b")
    assert verdict.stage == "selector"


# ---------------------------------------------------------------------------
# Integration with the Ollama client generate path: no silent fallback
# ---------------------------------------------------------------------------
def _make_ollama_client(fake_client):
    from src.backend.languagemodels.llm_interface import OllamaClient

    instance = OllamaClient.__new__(OllamaClient)
    instance.client = fake_client
    instance.default_model = "gemma4:31b"
    instance.host = "http://localhost:11434"
    return instance


def test_generate_blocks_oversized_model_without_calling_chat(monkeypatch):
    capacity = HostCapacity(
        total_bytes=16 * _GIB, available_bytes=8 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:31b", 47 * _GIB, "metadata", "test")
    monkeypatch.setattr(preflight, "detect_host_capacity", lambda **_: capacity)
    monkeypatch.setattr(
        preflight, "estimate_model_footprint", lambda *a, **k: footprint
    )
    fake = _FakeOllamaClient()
    client = _make_ollama_client(fake)

    with pytest.raises(ModelTooLargeForHostError):
        client.generate("hello", model="gemma4:31b")

    # No silent fallback: the underlying runtime chat must never be invoked.
    assert fake.chat_calls == []


def test_generate_does_not_block_on_heuristic_only_oversized_model(monkeypatch):
    # A large model name with no runtime metadata yields only a heuristic
    # estimate, which is advisory-only: generate must NOT be blocked by it, so
    # downstream behaviour (e.g. auto-pull) is preserved.
    capacity = HostCapacity(
        total_bytes=16 * _GIB, available_bytes=8 * _GIB, source="test"
    )
    monkeypatch.setattr(preflight, "detect_host_capacity", lambda **_: capacity)
    monkeypatch.delenv("VON_LOCAL_MODEL_PREFLIGHT_BLOCK_ON_HEURISTIC", raising=False)
    # No metadata methods configured -> estimator falls back to the heuristic.
    fake = _FakeOllamaClient(chat_response={"message": {"content": "ok then"}})
    client = _make_ollama_client(fake)

    result = client.generate("hi", model="llama3.3:70b")
    assert result == "ok then"
    assert fake.chat_calls == ["llama3.3:70b"]


def test_generate_proceeds_when_model_fits(monkeypatch):
    capacity = HostCapacity(
        total_bytes=64 * _GIB, available_bytes=40 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:e4b", 10 * _GIB, "metadata", "test")
    monkeypatch.setattr(preflight, "detect_host_capacity", lambda **_: capacity)
    monkeypatch.setattr(
        preflight, "estimate_model_footprint", lambda *a, **k: footprint
    )
    fake = _FakeOllamaClient(chat_response={"message": {"content": "hello there"}})
    client = _make_ollama_client(fake)

    result = client.generate("hi", model="gemma4:e4b")
    assert result == "hello there"
    assert fake.chat_calls == ["gemma4:e4b"]


def test_sensitive_model_call_bypasses_raw_response_cache(monkeypatch):
    from src.backend.languagemodels.llm_interface import (
        suppress_raw_llm_io_logging,
    )
    from src.backend.services import llm_response_cache_service as response_cache

    monkeypatch.setenv("VON_LLM_RESPONSE_CACHE", "on")
    monkeypatch.setattr(response_cache, "_persistent_collection", lambda: None)
    monkeypatch.setattr(
        preflight,
        "enforce_local_model_preflight",
        lambda *_args, **_kwargs: None,
    )
    response_cache.clear_llm_response_cache()
    fake = _FakeOllamaClient(
        chat_response={"message": {"content": "private model response"}}
    )
    client = _make_ollama_client(fake)

    with suppress_raw_llm_io_logging():
        result = client.generate(
            "unique private spreadsheet evidence for cache bypass",
            model="private-test-model",
        )

    assert result == "private model response"
    assert fake.chat_calls == ["private-test-model"]
    stats = response_cache.get_llm_response_cache_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 0
    assert stats["stores"] == 0
    assert stats["entries_in_memory"] == 0


# ---------------------------------------------------------------------------
# Host capacity detection + user message
# ---------------------------------------------------------------------------
def test_detect_host_capacity_returns_positive_totals():
    capacity = detect_host_capacity(use_cache=False)
    # On any normal host psutil reports a positive total.
    assert capacity.total_bytes is None or capacity.total_bytes > 0
    if capacity.total_bytes is not None:
        assert capacity.usable_bytes is not None
        assert capacity.usable_bytes <= capacity.total_bytes


def test_build_user_message_names_model_and_capacity():
    capacity = HostCapacity(
        total_bytes=16 * _GIB, available_bytes=8 * _GIB, source="test"
    )
    footprint = ModelFootprint("gemma4:31b", 47 * _GIB, "metadata", "test")
    verdict = preflight_local_model_fits(
        "gemma4:31b", stage="plain_response", capacity=capacity, footprint=footprint
    )
    message = build_user_message(verdict)
    assert "gemma4:31b" in message
    assert "plain_response" in message
    assert "GB" in message
