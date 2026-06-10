import json
import signal
from pathlib import Path

import pytest

from scripts import replay_llm_exchange as replay


def test_load_exchange_from_json_file_blob_body(tmp_path: Path) -> None:
    exchange_path = tmp_path / "exchange.json"
    exchange_path.write_text(
        json.dumps(
            {
                "schema_version": "llm_exchange_blob.v1",
                "stage": "planner",
                "workflow_stage_id": "plan_step",
                "model": "qwen3:8b",
                "provider": "ollama",
                "request": {
                    "prompt": "Plan this.",
                    "context": [{"role": "system", "content": "Use the plan."}],
                },
                "response": "Old response.",
            }
        ),
        encoding="utf-8",
    )

    exchange = replay.load_exchange_from_json_file(exchange_path)

    assert exchange.prompt == "Plan this."
    assert exchange.context == [{"role": "system", "content": "Use the plan."}]
    assert exchange.original_response == "Old response."
    assert exchange.metadata["source_kind"] == "exchange_blob"
    assert exchange.metadata["stage"] == "planner"


def test_load_failed_exchange_blob_surfaces_failure_metadata(tmp_path: Path) -> None:
    exchange_path = tmp_path / "failed_exchange.json"
    exchange_path.write_text(
        json.dumps(
            {
                "schema_version": "llm_exchange_blob.v1",
                "stage": "response_finalising",
                "model": "qwen3:8b",
                "provider": "ollama",
                "request": {"prompt": "Prompt that timed out.", "context": None},
                "response": None,
                "extra": {
                    "status": "failed",
                    "success": False,
                    "failure": {
                        "error": "LLM timed out after 120s",
                        "error_class": "TimeoutError",
                        "failure_kind": "candidate_error",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    exchange = replay.load_exchange_from_json_file(exchange_path)

    assert exchange.prompt == "Prompt that timed out."
    assert exchange.original_response is None
    assert exchange.metadata["status"] == "failed"
    assert exchange.metadata["success"] is False
    assert exchange.metadata["error_class"] == "TimeoutError"
    assert exchange.metadata["failure_kind"] == "candidate_error"


def test_main_inspect_only_supports_explicit_prompt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = replay.main(
        ["--prompt", "Try the revised prompt.", "--inspect-only", "--json"]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 0
    assert report["mode"] == "inspect_only"
    assert report["selected_exchange"]["metadata"]["source_kind"] == "explicit_prompt"
    assert report["selected_exchange"]["prompt_chars"] == len("Try the revised prompt.")
    assert report["replay"]["attempts"] == []


def test_mock_provider_replays_explicit_prompt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = replay.main(
        [
            "--prompt",
            "Measure this prompt.",
            "--provider",
            "mock",
            "--model",
            "mock-model",
            "--repeat",
            "2",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 0
    assert [attempt["status"] for attempt in report["replay"]["attempts"]] == [
        "ok",
        "ok",
    ]
    assert all(
        "prompt_chars=20" in attempt["response"]
        for attempt in report["replay"]["attempts"]
    )


def test_mock_provider_timeout_reports_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        pytest.skip("signal timer timeout support is unavailable on this platform")

    exit_code = replay.main(
        [
            "--prompt",
            "Slow prompt.",
            "--provider",
            "mock",
            "--model",
            "mock-model",
            "--mock-delay-sec",
            "0.2",
            "--timeout",
            "0.05",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 1
    attempt = report["replay"]["attempts"][0]
    assert attempt["status"] == "timeout"
    assert attempt["error_class"] == "ReplayTimeoutError"


def test_request_id_loader_hydrates_exchange_blob_ref() -> None:
    def fake_loader(**_kwargs: object) -> dict[str, object]:
        return {
            "namespace": "test-namespace",
            "entries": [
                {
                    "sequence_no": 1,
                    "stage": "planner",
                    "exchange_blob_ref": {"path": "blob.json"},
                }
            ],
        }

    def fake_blob_reader(_blob_ref: object) -> dict[str, object]:
        return {
            "schema_version": "llm_exchange_blob.v1",
            "stage": "planner",
            "request": {
                "prompt": "Captured prompt.",
                "context": [{"role": "user", "content": "Captured context."}],
            },
            "response": "Captured response.",
        }

    exchange = replay.load_exchange_from_request_id(
        request_id="request-123",
        call_log_loader=fake_loader,
        blob_reader=fake_blob_reader,
    )

    assert exchange.prompt == "Captured prompt."
    assert exchange.context == [{"role": "user", "content": "Captured context."}]
    assert exchange.metadata["request_id"] == "request-123"
    assert exchange.metadata["namespace"] == "test-namespace"
    assert exchange.metadata["source_kind"] == "exchange_blob"


def test_prompt_override_preserves_loaded_context_for_fix_planning() -> None:
    exchange = replay.LoadedExchange(
        prompt="Original poorly performing prompt.",
        context=[{"role": "system", "content": "Keep this captured context."}],
        original_response="Original response.",
        metadata={"source_kind": "exchange_blob", "stage": "answer"},
    )

    overridden = replay.apply_prompt_override(
        exchange,
        prompt="Revised prompt under diagnostic test.",
    )

    assert overridden.prompt == "Revised prompt under diagnostic test."
    assert overridden.context == exchange.context
    assert overridden.original_response == "Original response."
    assert overridden.metadata["prompt_override"]["source_kind"] == (
        "explicit_prompt_override"
    )
    assert overridden.metadata["prompt_override"]["diagnostic_purpose"] == (
        "prompt_fix_planning"
    )
