"""Unit tests for the LLM exchange blob writer (JVNAUTOSCI-2144)."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any, Iterator, Mapping
from unittest import mock

import pytest

from src.backend.services.blob_store import BlobRef, LocalBlobStore
from src.backend.services.llm_exchange_blob_writer import (
    DEFAULT_KEY_PREFIX,
    DEFAULT_SIZE_CAP_BYTES,
    SCHEMA_VERSION,
    LlmExchangeBlobWriter,
    get_llm_exchange_blob_writer_from_env,
    reset_llm_exchange_blob_writer_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_writer_singleton() -> Iterator[None]:
    reset_llm_exchange_blob_writer_singleton_for_tests()
    yield
    reset_llm_exchange_blob_writer_singleton_for_tests()


def _read_local_blob(store: LocalBlobStore, ref: dict[str, Any]) -> dict[str, Any]:
    raw = store.get_bytes(ref["key"])
    decompressed = gzip.decompress(raw)
    return json.loads(decompressed.decode("utf-8"))


def test_write_round_trip_local_store(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    writer = LlmExchangeBlobWriter(store=store)

    result = writer.write(
        turn_execution_id="turn-abc",
        stage="planner",
        workflow_stage_id="step-1",
        call_type="llm.generate",
        model="gpt-test",
        provider="openai",
        request_prompt="Hello, model.",
        request_context=[{"role": "user", "content": "Hi"}],
        response="Hello back.",
        prepared_at_utc="2024-01-01T00:00:00Z",
        sent_at_utc="2024-01-01T00:00:01Z",
        first_output_at_utc="2024-01-01T00:00:02Z",
        usage={"prompt_tokens": 5, "completion_tokens": 2},
    )

    assert "error" not in result
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["backend"] == "local"
    assert result["truncated"] is False
    assert result["key"].startswith(f"{DEFAULT_KEY_PREFIX}/")
    assert result["key"].endswith(".json.gz")
    assert result["size_bytes"] > 0
    assert result["uncompressed_size_bytes"] > 0

    body = _read_local_blob(store, result)
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["turn_execution_id"] == "turn-abc"
    assert body["stage"] == "planner"
    assert body["workflow_stage_id"] == "step-1"
    assert body["call_type"] == "llm.generate"
    assert body["model"] == "gpt-test"
    assert body["provider"] == "openai"
    assert body["request"]["prompt"] == "Hello, model."
    assert body["request"]["context"] == [{"role": "user", "content": "Hi"}]
    assert body["response"] == "Hello back."
    assert body["usage"] == {"prompt_tokens": 5, "completion_tokens": 2}


def test_write_size_cap_emits_truncated_metadata(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    writer = LlmExchangeBlobWriter(store=store, size_cap_bytes=2048)

    huge_response = "x" * 10_000

    result = writer.write(
        turn_execution_id="turn-big",
        stage="responder",
        workflow_stage_id=None,
        call_type="llm.generate",
        model="gpt-test",
        provider="openai",
        request_prompt="prompt",
        request_context=None,
        response=huge_response,
    )

    assert "error" not in result
    assert result["truncated"] is True
    assert result["truncation_reason"] == "size_cap"
    assert result["response_size_bytes"] >= 10_000

    body = _read_local_blob(store, result)
    assert body["truncated"] is True
    assert body["truncation_reason"] == "size_cap"
    assert "request" not in body
    assert "response" not in body
    assert body["response_size_bytes"] >= 10_000
    assert body["size_cap_bytes"] == 2048


def test_write_failure_isolates_error(tmp_path: Path) -> None:
    class _RaisingStore:
        def put_bytes(
            self,
            key: str,
            data: bytes,
            *,
            content_type: str | None = None,
            metadata: Mapping[str, str] | None = None,
        ) -> BlobRef:
            raise RuntimeError("simulated upload failure")

    writer = LlmExchangeBlobWriter(store=_RaisingStore())  # type: ignore[arg-type]

    result = writer.write(
        turn_execution_id="turn-x",
        stage="responder",
        workflow_stage_id=None,
        call_type="llm.generate",
        model="gpt-test",
        provider="openai",
        request_prompt="p",
        request_context=None,
        response="r",
    )

    assert "error" in result
    assert "simulated upload failure" in result["error"]
    assert "key" in result


def test_disabled_env_returns_no_writer(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {"VON_LLM_EXCHANGE_DISABLE": "1"}, clear=False):
        assert get_llm_exchange_blob_writer_from_env() is None


def test_factory_uses_env_local_root(tmp_path: Path) -> None:
    env = {
        "VON_BLOB_STORE_BACKEND": "local",
        "VON_LLM_EXCHANGE_LOCAL_ROOT": str(tmp_path),
    }
    # Make sure DISABLE isn't lingering from another test's environment.
    env["VON_LLM_EXCHANGE_DISABLE"] = ""
    with mock.patch.dict(os.environ, env, clear=False):
        writer = get_llm_exchange_blob_writer_from_env()
        assert writer is not None
        result = writer.write(
            turn_execution_id="turn-env",
            stage="env_stage",
            workflow_stage_id=None,
            call_type="llm.generate",
            model="gpt-test",
            provider="openai",
            request_prompt="p",
            request_context=None,
            response="r",
        )
        assert "error" not in result
        assert result["backend"] == "local"
        # The blob file should land under the configured root.
        on_disk = tmp_path / result["key"]
        assert on_disk.exists()


def test_default_size_cap_is_five_megabytes() -> None:
    assert DEFAULT_SIZE_CAP_BYTES == 5 * 1024 * 1024


def test_key_includes_sanitised_segments(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    writer = LlmExchangeBlobWriter(store=store)
    result = writer.write(
        turn_execution_id="turn/with:weird*chars",
        stage="some stage with spaces!",
        workflow_stage_id=None,
        call_type="llm.generate",
        model="m",
        provider="p",
        request_prompt="x",
        request_context=None,
        response="y",
    )
    assert "error" not in result
    # No raw special characters survive in the key (other than '/' separators).
    forbidden = ":*! "
    segment_after_prefix = result["key"][len(DEFAULT_KEY_PREFIX) + 1 :]
    for ch in forbidden:
        assert ch not in segment_after_prefix
