"""Replay one persisted or explicit LLM exchange against a selected model.

This script is a diagnostic support surface. It reuses a recorded prompt and
context from Von LLM telemetry, or a deliberately supplied prompt for prompt
fix planning, calls one chosen provider/model, and reports timing/error
details. It does not execute tools, run workflows, or mutate Vontology state.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCHEMA_VERSION = "llm_exchange_replay_report.v1"
EXCHANGE_SCHEMA_VERSION = "llm_exchange_replay_exchange.v1"
DEFAULT_LIMIT = 200


class ReplayInputError(RuntimeError):
    """Raised when the requested logged exchange cannot be loaded."""


class ReplayTimeoutError(TimeoutError):
    """Raised when a provider call exceeds the requested diagnostic timeout."""


@dataclass(frozen=True)
class LoadedExchange:
    prompt: Any
    context: Any
    original_response: Any
    metadata: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except Exception:
        return str(value)


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def _text_for_llm(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(value)


def _context_for_llm(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    context: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        role = _safe_str(item.get("role")) or "user"
        content = item.get("content")
        if content is None:
            continue
        context.append({"role": role, "content": _text_for_llm(content)})
    return context or None


def _extract_text_capture_text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        text = _safe_str(value.get("text"))
        if text is not None:
            return text
    return _safe_str(value)


def _exchange_from_blob_body(
    body: Mapping[str, Any],
    *,
    source: str,
    entry: Mapping[str, Any] | None = None,
) -> LoadedExchange:
    request_body = body.get("request")
    request_mapping = request_body if isinstance(request_body, Mapping) else {}
    extra = body.get("extra")
    extra_mapping = extra if isinstance(extra, Mapping) else {}
    failure = extra_mapping.get("failure")
    failure_mapping = failure if isinstance(failure, Mapping) else extra_mapping
    metadata = {
        "schema_version": EXCHANGE_SCHEMA_VERSION,
        "source": source,
        "source_kind": "exchange_blob",
        "turn_execution_id": _safe_str(body.get("turn_execution_id")),
        "stage": _safe_str(body.get("stage")),
        "workflow_stage_id": _safe_str(body.get("workflow_stage_id")),
        "call_type": _safe_str(body.get("call_type")),
        "model": _safe_str(body.get("model")),
        "provider": _safe_str(body.get("provider")),
        "captured_at_utc": _safe_str(body.get("captured_at_utc")),
        "prepared_at_utc": _safe_str(body.get("prepared_at_utc")),
        "sent_at_utc": _safe_str(body.get("sent_at_utc")),
        "first_output_at_utc": _safe_str(body.get("first_output_at_utc")),
        "blob_truncated": bool(body.get("truncated")),
        "status": _safe_str(body.get("status"))
        or _safe_str(extra_mapping.get("status")),
        "success": (
            extra_mapping.get("success")
            if isinstance(extra_mapping.get("success"), bool)
            else None
        ),
        "error": _safe_str(failure_mapping.get("error")),
        "error_class": _safe_str(failure_mapping.get("error_class")),
        "failure_kind": _safe_str(failure_mapping.get("failure_kind")),
    }
    if entry:
        metadata["entry"] = {
            "sequence_no": entry.get("sequence_no"),
            "source": entry.get("source"),
            "source_index": entry.get("source_index"),
        }
        for key in ("status", "success", "error", "error_class", "failure_kind"):
            if metadata.get(key) is None and entry.get(key) is not None:
                metadata[key] = entry.get(key)
    return LoadedExchange(
        prompt=request_mapping.get("prompt"),
        context=request_mapping.get("context"),
        original_response=body.get("response"),
        metadata={key: item for key, item in metadata.items() if item is not None},
    )


def _exchange_from_log_entry(
    entry: Mapping[str, Any], *, source: str
) -> LoadedExchange:
    prompt_text = _extract_text_capture_text(entry.get("prompt"))
    response_text = _extract_text_capture_text(entry.get("response"))
    if not prompt_text:
        raise ReplayInputError("selected entry does not contain a replayable prompt")
    metadata = {
        "schema_version": EXCHANGE_SCHEMA_VERSION,
        "source": source,
        "source_kind": "llm_call_log_entry",
        "sequence_no": entry.get("sequence_no"),
        "call_type": _safe_str(entry.get("call_type")),
        "stage": _safe_str(entry.get("stage")),
        "workflow_stage_id": _safe_str(entry.get("workflow_stage_id")),
        "model": _safe_str(entry.get("model")),
        "provider": _safe_str(entry.get("provider")),
        "duration_ms": entry.get("duration_ms"),
        "status": _safe_str(entry.get("status")),
        "success": (
            entry.get("success") if isinstance(entry.get("success"), bool) else None
        ),
        "error": _safe_str(entry.get("error")),
        "error_class": _safe_str(entry.get("error_class")),
        "failure_kind": _safe_str(entry.get("failure_kind")),
        "at_utc": _safe_str(entry.get("at_utc")),
        "prompt_recorded": bool(entry.get("prompt_recorded")),
        "response_recorded": bool(entry.get("response_recorded")),
        "exchange_source": _safe_str(entry.get("exchange_source")),
    }
    return LoadedExchange(
        prompt=prompt_text,
        context=None,
        original_response=response_text,
        metadata={key: item for key, item in metadata.items() if item is not None},
    )


def load_exchange_from_json_file(path: Path) -> LoadedExchange:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReplayInputError(f"could not read exchange JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ReplayInputError("exchange JSON must contain an object")

    if payload.get("schema_version") == "llm_exchange_blob.v1":
        return _exchange_from_blob_body(payload, source=str(path))

    if isinstance(payload.get("request"), Mapping):
        return _exchange_from_blob_body(
            {
                "schema_version": "llm_exchange_blob.v1",
                **dict(payload),
            },
            source=str(path),
        )

    prompt = payload.get("prompt")
    if prompt is None:
        raise ReplayInputError("exchange JSON must contain request.prompt or prompt")
    return LoadedExchange(
        prompt=prompt,
        context=payload.get("context"),
        original_response=payload.get("response"),
        metadata={
            "schema_version": EXCHANGE_SCHEMA_VERSION,
            "source": str(path),
            "source_kind": "plain_exchange_json",
            "stage": _safe_str(payload.get("stage")),
            "workflow_stage_id": _safe_str(payload.get("workflow_stage_id")),
            "model": _safe_str(payload.get("model")),
            "provider": _safe_str(payload.get("provider")),
        },
    )


def load_exchange_from_explicit_prompt(
    *,
    prompt: str | None = None,
    prompt_file: Path | None = None,
    context_json: Path | None = None,
) -> LoadedExchange:
    if prompt_file is not None:
        try:
            prompt_value = prompt_file.read_text(encoding="utf-8")
        except Exception as exc:
            raise ReplayInputError(f"could not read prompt file: {exc}") from exc
        source = str(prompt_file)
    else:
        prompt_value = prompt or ""
        source = "explicit_prompt"
    if not prompt_value.strip():
        raise ReplayInputError("explicit prompt is empty")

    context_value: Any = None
    if context_json is not None:
        try:
            context_value = json.loads(context_json.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ReplayInputError(f"could not read context JSON: {exc}") from exc

    return LoadedExchange(
        prompt=prompt_value,
        context=context_value,
        original_response=None,
        metadata={
            "schema_version": EXCHANGE_SCHEMA_VERSION,
            "source": source,
            "source_kind": "explicit_prompt",
            "context_source": str(context_json) if context_json is not None else None,
        },
    )


def apply_prompt_override(
    exchange: LoadedExchange,
    *,
    prompt: str | None = None,
    prompt_file: Path | None = None,
) -> LoadedExchange:
    if prompt_file is not None:
        try:
            prompt_value = prompt_file.read_text(encoding="utf-8")
        except Exception as exc:
            raise ReplayInputError(
                f"could not read override prompt file: {exc}"
            ) from exc
        source = str(prompt_file)
    else:
        prompt_value = prompt or ""
        source = "explicit_override_prompt"
    if not prompt_value.strip():
        raise ReplayInputError("override prompt is empty")

    metadata = dict(exchange.metadata)
    metadata["prompt_override"] = {
        "source": source,
        "source_kind": "explicit_prompt_override",
        "diagnostic_purpose": "prompt_fix_planning",
        "applied_at_utc": _utc_now(),
        "original_prompt_chars": len(_text_for_llm(exchange.prompt)),
        "override_prompt_chars": len(prompt_value),
    }
    return LoadedExchange(
        prompt=prompt_value,
        context=exchange.context,
        original_response=exchange.original_response,
        metadata=metadata,
    )


def _entry_matches(
    entry: Mapping[str, Any],
    *,
    stage: str | None,
    workflow_stage_id: str | None,
    call_type: str | None,
) -> bool:
    if stage and _safe_str(entry.get("stage")) != stage:
        return False
    if (
        workflow_stage_id
        and _safe_str(entry.get("workflow_stage_id")) != workflow_stage_id
    ):
        return False
    if call_type and _safe_str(entry.get("call_type")) != call_type:
        return False
    return True


def load_exchange_from_request_id(
    *,
    request_id: str,
    namespace: str | None = None,
    entry_index: int = 1,
    stage: str | None = None,
    workflow_stage_id: str | None = None,
    call_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
    call_log_loader: Callable[..., Mapping[str, Any] | None] | None = None,
    blob_reader: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> LoadedExchange:
    if not request_id.strip():
        raise ReplayInputError("request_id is required")
    if entry_index < 1:
        raise ReplayInputError("entry index is 1-based and must be positive")
    if call_log_loader is None:
        from src.backend.services.turn_execution_diagnostics_service import (
            get_turn_llm_call_log_payload,
        )

        call_log_loader = get_turn_llm_call_log_payload
    payload = call_log_loader(
        request_id=request_id,
        namespace=namespace,
        offset=0,
        limit=max(1, min(int(limit), 200)),
    )
    if not isinstance(payload, Mapping):
        raise ReplayInputError(f"no LLM call log found for request_id={request_id}")
    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, Sequence) or isinstance(
        entries_raw, (str, bytes, bytearray)
    ):
        raise ReplayInputError(
            f"LLM call log for request_id={request_id} has no entries"
        )
    entries = [
        entry
        for entry in entries_raw
        if isinstance(entry, Mapping)
        and _entry_matches(
            entry,
            stage=stage,
            workflow_stage_id=workflow_stage_id,
            call_type=call_type,
        )
    ]
    if not entries:
        raise ReplayInputError("no LLM call log entries matched the requested filters")
    if entry_index > len(entries):
        raise ReplayInputError(
            f"entry index {entry_index} out of range for {len(entries)} matched entries"
        )
    selected = entries[entry_index - 1]
    blob_ref = selected.get("exchange_blob_ref")
    if isinstance(blob_ref, Mapping) and blob_ref:
        if blob_reader is None:
            from src.backend.services.llm_exchange_blob_writer import (
                read_llm_exchange_blob_ref,
            )

            blob_reader = read_llm_exchange_blob_ref
        body = blob_reader(blob_ref)
        if isinstance(body, Mapping):
            loaded = _exchange_from_blob_body(
                body,
                source=f"request_id:{request_id}",
                entry=selected,
            )
            loaded.metadata["request_id"] = request_id
            loaded.metadata["namespace"] = namespace or payload.get("namespace")
            loaded.metadata["selected_entry_index"] = entry_index
            loaded.metadata["matched_entry_count"] = len(entries)
            return loaded
    loaded = _exchange_from_log_entry(selected, source=f"request_id:{request_id}")
    loaded.metadata["request_id"] = request_id
    loaded.metadata["namespace"] = namespace or payload.get("namespace")
    loaded.metadata["selected_entry_index"] = entry_index
    loaded.metadata["matched_entry_count"] = len(entries)
    return loaded


def build_exchange_summary(exchange: LoadedExchange) -> dict[str, Any]:
    prompt_text = _text_for_llm(exchange.prompt)
    context = _context_for_llm(exchange.context)
    original_response_text = (
        None
        if exchange.original_response is None
        else _text_for_llm(exchange.original_response)
    )
    return {
        "metadata": dict(exchange.metadata),
        "prompt_chars": len(prompt_text),
        "prompt_json_chars": _json_size(exchange.prompt),
        "context_message_count": len(context or []),
        "context_json_chars": _json_size(exchange.context),
        "original_response_chars": (
            len(original_response_text) if original_response_text is not None else None
        ),
    }


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv  # type: ignore

        dotenv_path = find_dotenv(usecwd=True)
        if dotenv_path:
            load_dotenv(dotenv_path, override=False)
    except Exception:
        return


def _client_for_provider(
    provider: str,
    *,
    ollama_host: str | None,
    mock_delay_sec: float,
    mock_error: str | None,
) -> Any:
    provider_key = provider.strip().lower()
    if provider_key == "mock":
        return _MockReplayClient(delay_sec=mock_delay_sec, error=mock_error)
    if provider_key == "ollama":
        from src.backend.languagemodels.llm_interface import OllamaClient

        return OllamaClient(host=ollama_host)
    if provider_key == "openai":
        from src.backend.languagemodels.llm_interface import OpenAIClient

        return OpenAIClient()
    if provider_key == "gemini":
        from src.backend.languagemodels.llm_interface import GeminiClient

        return GeminiClient()
    raise ReplayInputError(f"unsupported provider: {provider}")


class _MockReplayClient:
    def __init__(self, *, delay_sec: float = 0.0, error: str | None = None) -> None:
        self._delay_sec = max(0.0, float(delay_sec or 0.0))
        self._error = error

    def generate(
        self,
        prompt: str,
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        llm_params: Mapping[str, Any] | None = None,
    ) -> str:
        if self._delay_sec:
            time.sleep(self._delay_sec)
        if self._error:
            raise RuntimeError(self._error)
        return (
            f"mock replay response model={model or 'mock-model'} "
            f"prompt_chars={len(prompt)} context_messages={len(context or [])}"
        )


def _call_with_timeout(
    call: Callable[[], str],
    *,
    timeout_sec: float | None,
) -> str:
    if timeout_sec is None or timeout_sec <= 0:
        return call()
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return call()

    def _handle_timeout(_signum: int, _frame: Any) -> None:
        raise ReplayTimeoutError(f"LLM replay timed out after {timeout_sec:g}s")

    old_handler = signal.getsignal(signal.SIGALRM)
    try:
        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, float(timeout_sec))
        return call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)


def replay_once(
    exchange: LoadedExchange,
    *,
    provider: str,
    model: str,
    timeout_sec: float | None,
    temperature: float | None,
    num_predict: int | None,
    ollama_host: str | None = None,
    mock_delay_sec: float = 0.0,
    mock_error: str | None = None,
    attempt_no: int = 1,
) -> dict[str, Any]:
    _load_dotenv_if_available()
    prompt = _text_for_llm(exchange.prompt)
    context = _context_for_llm(exchange.context)
    llm_params: dict[str, Any] = {}
    if temperature is not None:
        llm_params["temperature"] = float(temperature)
    if num_predict is not None:
        llm_params["num_predict"] = int(num_predict)
    client = _client_for_provider(
        provider,
        ollama_host=ollama_host,
        mock_delay_sec=mock_delay_sec,
        mock_error=mock_error,
    )
    started = time.perf_counter()
    started_at = _utc_now()
    try:
        response = _call_with_timeout(
            lambda: client.generate(
                prompt,
                context=context,
                model=model,
                llm_params=llm_params or None,
            ),
            timeout_sec=timeout_sec,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "attempt_no": attempt_no,
            "provider": provider,
            "model": model,
            "status": "ok",
            "started_at_utc": started_at,
            "completed_at_utc": _utc_now(),
            "duration_ms": duration_ms,
            "timeout_sec": timeout_sec,
            "response": response,
            "response_chars": len(response),
        }
    except ReplayTimeoutError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "attempt_no": attempt_no,
            "provider": provider,
            "model": model,
            "status": "timeout",
            "started_at_utc": started_at,
            "completed_at_utc": _utc_now(),
            "duration_ms": duration_ms,
            "timeout_sec": timeout_sec,
            "error_class": type(exc).__name__,
            "error": str(exc),
        }
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "attempt_no": attempt_no,
            "provider": provider,
            "model": model,
            "status": "error",
            "started_at_utc": started_at,
            "completed_at_utc": _utc_now(),
            "duration_ms": duration_ms,
            "timeout_sec": timeout_sec,
            "error_class": type(exc).__name__,
            "error": str(exc),
        }


def build_report(
    exchange: LoadedExchange,
    *,
    provider: str | None,
    model: str | None,
    timeout_sec: float | None,
    repeat: int,
    attempts: Sequence[Mapping[str, Any]],
    inspect_only: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": _utc_now(),
        "mode": "inspect_only" if inspect_only else "replay",
        "policy_boundary": {
            "diagnostic_only": True,
            "executes_tools": False,
            "runs_workflows": False,
            "mutates_vontology": False,
            "acceptance_scope": ("single_llm_exchange_only_not_full_turn_acceptance"),
        },
        "selected_exchange": build_exchange_summary(exchange),
        "replay": {
            "provider": provider,
            "model": model,
            "timeout_sec": timeout_sec,
            "repeat": repeat,
            "attempt_count": len(attempts),
            "attempts": [dict(item) for item in attempts],
        },
    }


def _print_report(report: Mapping[str, Any]) -> None:
    selected = report.get("selected_exchange")
    selected = selected if isinstance(selected, Mapping) else {}
    metadata = selected.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    replay = report.get("replay")
    replay = replay if isinstance(replay, Mapping) else {}
    print("Selected LLM exchange")
    print(f"  source: {metadata.get('source') or 'unknown'}")
    print(f"  stage: {metadata.get('stage') or '-'}")
    print(f"  workflow_stage_id: {metadata.get('workflow_stage_id') or '-'}")
    print(
        f"  original provider/model: {metadata.get('provider') or '-'}/{metadata.get('model') or '-'}"
    )
    print(f"  prompt chars: {selected.get('prompt_chars')}")
    print(f"  context messages: {selected.get('context_message_count')}")
    print(f"  context JSON chars: {selected.get('context_json_chars')}")
    print(f"  original response chars: {selected.get('original_response_chars')}")
    if report.get("mode") == "inspect_only":
        print("Inspect only: no model call made.")
        return
    print("Replay attempts")
    for attempt in replay.get("attempts") or []:
        if not isinstance(attempt, Mapping):
            continue
        line = (
            f"  #{attempt.get('attempt_no')} {attempt.get('provider')}/"
            f"{attempt.get('model')} {attempt.get('status')} "
            f"{attempt.get('duration_ms')}ms"
        )
        if attempt.get("response_chars") is not None:
            line += f" response_chars={attempt.get('response_chars')}"
        if attempt.get("error"):
            line += f" error={attempt.get('error')}"
        print(line)


def _write_outputs(
    *,
    report: Mapping[str, Any],
    output_path: Path | None,
    jsonl_path: Path | None,
) -> None:
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a", encoding="utf-8") as handle:
            for attempt in report.get("replay", {}).get("attempts", []):
                handle.write(
                    json.dumps(
                        {
                            "schema_version": "llm_exchange_replay_attempt.v1",
                            "created_at_utc": report.get("created_at_utc"),
                            "selected_exchange": report.get("selected_exchange"),
                            "attempt": attempt,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay one logged Von LLM exchange against a selected model."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--request-id", help="Turn/request id to load from LLM call log."
    )
    source.add_argument(
        "--exchange-json",
        type=Path,
        help="Path to an llm_exchange_blob.v1 or simple prompt/context JSON file.",
    )
    source.add_argument("--prompt", help="Explicit prompt text to replay directly.")
    source.add_argument(
        "--prompt-file",
        type=Path,
        help="Path to explicit prompt text to replay directly.",
    )
    override = parser.add_mutually_exclusive_group()
    override.add_argument(
        "--override-prompt",
        help=(
            "Replace the loaded prompt while preserving loaded context/metadata. "
            "Use for diagnostic planning of a revised prompt body."
        ),
    )
    override.add_argument(
        "--override-prompt-file",
        type=Path,
        help=(
            "Path to a revised prompt body that replaces the loaded prompt while "
            "preserving loaded context/metadata."
        ),
    )
    parser.add_argument(
        "--context-json",
        type=Path,
        help="Optional context message JSON for --prompt/--prompt-file.",
    )
    parser.add_argument("--namespace", help="Optional namespace for request-id lookup.")
    parser.add_argument(
        "--entry", type=int, default=1, help="1-based matched entry index."
    )
    parser.add_argument("--stage", help="Filter logged entries by stage.")
    parser.add_argument("--workflow-stage-id", help="Filter by workflow stage id.")
    parser.add_argument("--call-type", help="Filter by call type.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--provider", choices=["ollama", "openai", "gemini", "mock"])
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--num-predict", type=int)
    parser.add_argument("--ollama-host")
    parser.add_argument("--mock-delay-sec", type=float, default=0.0)
    parser.add_argument("--mock-error")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jsonl-out", type=Path)
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    return parser


def run_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if (args.override_prompt is not None or args.override_prompt_file is not None) and (
        args.prompt is not None or args.prompt_file is not None
    ):
        raise ReplayInputError(
            "--override-prompt/--override-prompt-file are only valid with "
            "--request-id or --exchange-json"
        )
    if args.exchange_json is not None:
        exchange = load_exchange_from_json_file(args.exchange_json)
    elif args.prompt is not None or args.prompt_file is not None:
        exchange = load_exchange_from_explicit_prompt(
            prompt=args.prompt,
            prompt_file=args.prompt_file,
            context_json=args.context_json,
        )
    else:
        exchange = load_exchange_from_request_id(
            request_id=args.request_id,
            namespace=args.namespace,
            entry_index=args.entry,
            stage=args.stage,
            workflow_stage_id=args.workflow_stage_id,
            call_type=args.call_type,
            limit=args.limit,
        )
    if args.override_prompt is not None or args.override_prompt_file is not None:
        exchange = apply_prompt_override(
            exchange,
            prompt=args.override_prompt,
            prompt_file=args.override_prompt_file,
        )
    if args.inspect_only:
        return build_report(
            exchange,
            provider=args.provider,
            model=args.model,
            timeout_sec=args.timeout,
            repeat=max(1, int(args.repeat or 1)),
            attempts=[],
            inspect_only=True,
        )
    if not args.provider:
        raise ReplayInputError("--provider is required unless --inspect-only is used")
    if not args.model:
        raise ReplayInputError("--model is required unless --inspect-only is used")
    repeat = max(1, int(args.repeat or 1))
    attempts = [
        replay_once(
            exchange,
            provider=args.provider,
            model=args.model,
            timeout_sec=args.timeout,
            temperature=args.temperature,
            num_predict=args.num_predict,
            ollama_host=args.ollama_host,
            mock_delay_sec=args.mock_delay_sec,
            mock_error=args.mock_error,
            attempt_no=index,
        )
        for index in range(1, repeat + 1)
    ]
    return build_report(
        exchange,
        provider=args.provider,
        model=args.model,
        timeout_sec=args.timeout,
        repeat=repeat,
        attempts=attempts,
        inspect_only=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        report = run_from_args(args)
        _write_outputs(
            report=report,
            output_path=args.output,
            jsonl_path=args.jsonl_out,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            _print_report(report)
        statuses = [
            attempt.get("status")
            for attempt in report.get("replay", {}).get("attempts", [])
            if isinstance(attempt, Mapping)
        ]
        if statuses and all(status != "ok" for status in statuses):
            return 1
        return 0
    except ReplayInputError as exc:
        print(f"replay_llm_exchange: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
