"""Discover and run replay cases, then emit JSON and Markdown health reports.

This runner provides one place to:
- list discovered replay definitions from prompt banks and workflow seed bundles;
- run replay-capable cases with per-case and suite-level timeout bounds;
- report skipped/not-runnable/timeouts as first-class outcomes;
- enforce local-first defaults (AgentTest server and local models) unless
  explicit opt-in flags are provided.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.live_test_server_defaults import get_default_agent_test_base_url

PROMPT_BANK_PATH = PROJECT_ROOT / "scripts" / "live_kb_tool_prompt_bank.json"
PROMPT_SAMPLER_SCRIPT_PATH = (
    PROJECT_ROOT / "scripts" / "run_live_kb_tool_prompt_sampler.py"
)
ARXIV_WORKFLOW_SCRIPT_PATH = (
    PROJECT_ROOT / "scripts" / "run_live_arxiv_ingestion_workflow_test.py"
)
SEED_BUNDLE_DIR = PROJECT_ROOT / "src" / "backend" / "workflows" / "repo_seed_bundles"

REPORT_SCHEMA_VERSION = "replay_suite_report.v1"
DEFAULT_PROMPT_MODEL = "gemma4:31b"
PREMIUM_MODEL_PREFIXES = (
    "gpt-",
    "gpt4",
    "gpt5",
    "o1",
    "o3",
    "o4",
    "claude",
    "gemini",
    "text-davinci",
)

REPLAY_SOURCE_CHOICES = (
    "all",
    "prompt-bank",
    "arxiv-workflow",
    "email-arxiv-workflow",
    "failure-case-prompt",
    "synthetic-workflow-regression",
)

SEED_WORKFLOW_TARGETS: dict[str, dict[str, Any]] = {
    "#V#arxiv_paper_ingestion_testing_workflow": {
        "source_category": "arxiv-workflow",
        "source_label": "workflow seed bundle",
        "what_it_tests": "arXiv ingestion testing workflow via durable workflow API",
        "surface_exercised": "durable workflow API",
        "runnable": True,
        "required_workflows": [
            "#V#arxiv_paper_ingestion_testing_workflow",
        ],
    },
    "#V#arxiv_paper_representation_workflow": {
        "source_category": "arxiv-workflow",
        "source_label": "workflow seed bundle",
        "what_it_tests": "arXiv paper representation coverage and post-ingestion representation intent",
        "surface_exercised": "durable workflow API",
        "runnable": False,
        "required_workflows": [
            "#V#arxiv_paper_representation_workflow",
        ],
        "not_runnable_reason": (
            "No standalone replay harness is wired for this workflow yet; "
            "coverage is represented and reported separately."
        ),
    },
    "#V#zhan_gmail_arxiv_ingestion_workflow": {
        "source_category": "email-arxiv-workflow",
        "source_label": "workflow seed bundle",
        "what_it_tests": "Gmail-to-arXiv ingestion and representation convergence",
        "surface_exercised": "workflow seed bundle",
        "runnable": False,
        "required_workflows": [
            "#V#zhan_gmail_arxiv_ingestion_workflow",
            "#V#email_arxiv_ingestion_from_message_workflow",
        ],
        "not_runnable_reason": "No dedicated live replay runner is integrated for this workflow family yet.",
    },
    "#V#email_arxiv_ingestion_from_message_workflow": {
        "source_category": "email-arxiv-workflow",
        "source_label": "workflow seed bundle",
        "what_it_tests": "Single-message email-to-arXiv ingestion subworkflow",
        "surface_exercised": "workflow seed bundle",
        "runnable": False,
        "required_workflows": [
            "#V#email_arxiv_ingestion_from_message_workflow",
        ],
        "not_runnable_reason": "No dedicated live replay runner is integrated for this workflow family yet.",
    },
    "#V#failure_case_prompt_improvement_workflow": {
        "source_category": "failure-case-prompt",
        "source_label": "workflow seed bundle",
        "what_it_tests": "Failure-case prompt improvement replay path",
        "surface_exercised": "workflow seed bundle",
        "runnable": False,
        "required_workflows": [
            "#V#failure_case_prompt_improvement_workflow",
        ],
        "not_runnable_reason": "No dedicated live replay runner is integrated for this workflow yet.",
    },
    "#V#synthetic_workflow_regression_suite_workflow": {
        "source_category": "synthetic-workflow-regression",
        "source_label": "workflow seed bundle",
        "what_it_tests": "Synthetic workflow regression suite dispatch and verdict routing",
        "surface_exercised": "workflow seed bundle",
        "runnable": False,
        "required_workflows": [
            "#V#synthetic_workflow_regression_suite_workflow",
        ],
        "not_runnable_reason": "No dedicated live replay runner is integrated for this workflow yet.",
    },
}


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _load_json(path: Path) -> dict[str, Any]:
    return _as_mapping(json.loads(path.read_text(encoding="utf-8")))


def _looks_like_premium_model(model_name: str) -> bool:
    lowered = _safe_text(model_name).lower()
    if not lowered:
        return False
    if lowered.startswith("openai:") or lowered.startswith("anthropic:") or lowered.startswith("gemini:"):
        return True
    return any(lowered.startswith(prefix) for prefix in PREMIUM_MODEL_PREFIXES)


def _enforce_local_only_policy(model_name: str, allow_premium_model: bool) -> None:
    if allow_premium_model:
        return
    if _looks_like_premium_model(model_name):
        raise RuntimeError(
            "Local-only policy blocks premium model names by default; pass --allow-premium-model to opt in."
        )


def _collect_values_for_key(node: Any, key_name: str) -> list[str]:
    values: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, value in current.items():
                if key == key_name and isinstance(value, str):
                    values.append(value)
                stack.append(value)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            stack.extend(current)
    return values


def _discover_replay_or_rubric_concepts(bundle_payload: Mapping[str, Any]) -> list[str]:
    found: set[str] = set()
    stack = [bundle_payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for value in current.values():
                stack.append(value)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            stack.extend(current)
        elif isinstance(current, str) and current.startswith("#V#"):
            lowered = current.lower()
            if "replay" in lowered or "rubric" in lowered:
                found.add(current)
    return sorted(found)


def discover_prompt_bank_cases(
    *,
    prompt_complexity_classes: set[str] | None = None,
    prompt_ids: set[str] | None = None,
    max_prompt_cases: int | None = None,
) -> list[dict[str, Any]]:
    payload = _load_json(PROMPT_BANK_PATH)
    prompts = [entry for entry in _as_list(payload.get("prompts")) if isinstance(entry, Mapping)]
    discovered: list[dict[str, Any]] = []
    for prompt in prompts:
        prompt_id = _safe_text(prompt.get("id"))
        complexity_class = _safe_text(prompt.get("complexity_class"))
        if prompt_ids and prompt_id not in prompt_ids:
            continue
        if prompt_complexity_classes and complexity_class not in prompt_complexity_classes:
            continue
        required_concepts = {
            "#V#live_prompt_sampler_replay_evaluation_rubric_v1",
            *(
                _safe_text(item)
                for item in _as_list(prompt.get("required_concepts"))
                if _safe_text(item)
            ),
        }
        discovered.append(
            {
                "replay_id": f"prompt-bank:{prompt_id}",
                "source": "scripts/live_kb_tool_prompt_bank.json",
                "source_category": "prompt-bank",
                "source_reference": "scripts/live_kb_tool_prompt_bank.json",
                "what_it_tests": _safe_text(prompt.get("category"))
                or "prompt-bank replay",
                "surface_exercised": "/von/generate",
                "required_tools": sorted(
                    {
                        _safe_text(item)
                        for item in _as_list(prompt.get("likely_tools"))
                        if _safe_text(item)
                    }
                ),
                "required_workflows": sorted(
                    {
                        _safe_text(item)
                        for item in _as_list(prompt.get("required_workflows"))
                        if _safe_text(item)
                    }
                ),
                "required_concepts": sorted(required_concepts),
                "mode_environment": "AgentTest/local model",
                "prompt_id": prompt_id,
                "prompt_text": _safe_text(prompt.get("prompt")),
                "complexity_class": complexity_class,
                "runnable": True,
                "execution_kind": "prompt_sampler",
            }
        )
    if max_prompt_cases is not None and max_prompt_cases >= 0:
        return discovered[:max_prompt_cases]
    return discovered


def discover_seed_bundle_workflow_cases() -> list[dict[str, Any]]:
    discovered_by_replay_id: dict[str, dict[str, Any]] = {}
    bundle_paths = sorted(SEED_BUNDLE_DIR.glob("*seed_bundle*.json"))
    for bundle_path in bundle_paths:
        payload = _load_json(bundle_path)
        workflow_ids = set(_collect_values_for_key(payload, "workflow_id"))
        replay_concepts = _discover_replay_or_rubric_concepts(payload)
        for workflow_id, metadata in SEED_WORKFLOW_TARGETS.items():
            if workflow_id not in workflow_ids:
                continue
            replay_id = f"workflow:{workflow_id}"
            source_path = str(bundle_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
            existing = discovered_by_replay_id.get(replay_id)
            if existing is None:
                discovered_by_replay_id[replay_id] = {
                    "replay_id": replay_id,
                    "source": source_path,
                    "source_category": _safe_text(metadata.get("source_category")),
                    "source_reference": source_path,
                    "additional_sources": [],
                    "what_it_tests": _safe_text(metadata.get("what_it_tests")),
                    "surface_exercised": _safe_text(metadata.get("surface_exercised")),
                    "required_tools": [],
                    "required_workflows": list(_as_list(metadata.get("required_workflows"))),
                    "required_concepts": replay_concepts,
                    "mode_environment": "AgentTest/local workflow surfaces",
                    "workflow_id": workflow_id,
                    "runnable": bool(metadata.get("runnable")),
                    "execution_kind": "arxiv_workflow_api"
                    if workflow_id == "#V#arxiv_paper_ingestion_testing_workflow"
                    else "seed_only",
                    "not_runnable_reason": _safe_text(metadata.get("not_runnable_reason")),
                }
                continue

            additional_sources = {
                _safe_text(item)
                for item in _as_list(existing.get("additional_sources"))
                if _safe_text(item)
            }
            additional_sources.add(source_path)
            existing["additional_sources"] = sorted(additional_sources)
            merged_required_concepts = {
                _safe_text(item)
                for item in _as_list(existing.get("required_concepts"))
                if _safe_text(item)
            }
            merged_required_concepts.update(
                _safe_text(item) for item in replay_concepts if _safe_text(item)
            )
            existing["required_concepts"] = sorted(merged_required_concepts)
    return sorted(discovered_by_replay_id.values(), key=lambda item: _safe_text(item.get("replay_id")))


def discover_replay_cases(
    *,
    prompt_complexity_classes: set[str] | None = None,
    prompt_ids: set[str] | None = None,
    max_prompt_cases: int | None = None,
) -> list[dict[str, Any]]:
    cases = discover_prompt_bank_cases(
        prompt_complexity_classes=prompt_complexity_classes,
        prompt_ids=prompt_ids,
        max_prompt_cases=max_prompt_cases,
    )
    cases.extend(discover_seed_bundle_workflow_cases())
    return sorted(cases, key=lambda item: _safe_text(item.get("replay_id")))


def filter_replay_cases(
    cases: Sequence[Mapping[str, Any]],
    *,
    source_filters: set[str],
    replay_ids: set[str] | None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    include_all_sources = "all" in source_filters or not source_filters
    for case in cases:
        source_category = _safe_text(case.get("source_category"))
        replay_id = _safe_text(case.get("replay_id"))
        if replay_ids and replay_id not in replay_ids:
            continue
        if not include_all_sources and source_category not in source_filters:
            continue
        filtered.append(dict(case))
    return filtered


def _extract_evidence_tokens(payload: Any) -> list[str]:
    evidence: set[str] = set()
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, value in current.items():
                if key in {
                    "request_id",
                    "workflow_instance_id",
                    "instance_id",
                    "task_id",
                    "session_id",
                    "output_json",
                    "result_path",
                }:
                    text = _safe_text(value)
                    if text:
                        evidence.add(f"{key}={text}")
                stack.append(value)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            stack.extend(current)
    return sorted(evidence)


def _read_json_file_if_present(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _load_json(path)
    except Exception:
        return {}


def _parse_json_from_stdout(stdout_text: str) -> dict[str, Any]:
    text = _safe_text(stdout_text)
    if not text:
        return {}
    try:
        return _as_mapping(json.loads(text))
    except Exception:
        return {}


def _clean_sampler_note_prefix(message: str) -> str:
    text = _safe_text(message)
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned_lines = [line for line in lines if not line.startswith("NOTE:")]
    return "\n".join(cleaned_lines).strip()


def _extract_error_from_embedded_json(message: str) -> str:
    text = _safe_text(message)
    if not text:
        return ""
    start = text.find("{")
    if start < 0:
        return ""
    try:
        payload = _as_mapping(json.loads(text[start:]))
    except Exception:
        return ""
    return _safe_text(payload.get("error"))


def _normalise_prompt_sampler_error_text(message: str) -> str:
    raw_text = _safe_text(message)
    if not raw_text:
        return ""
    embedded = _extract_error_from_embedded_json(raw_text)
    if embedded:
        return embedded
    cleaned = _clean_sampler_note_prefix(raw_text)
    embedded_after_clean = _extract_error_from_embedded_json(cleaned)
    if embedded_after_clean:
        return embedded_after_clean
    return cleaned or raw_text


def _extract_prompt_sampler_failure_reason(
    payload: Mapping[str, Any],
    *,
    stderr_text: str,
    fallback: str,
) -> str:
    direct_error = _normalise_prompt_sampler_error_text(
        _safe_text(payload.get("error"))
    )
    if direct_error:
        return direct_error

    response = _as_mapping(payload.get("response"))
    failure = _as_mapping(response.get("failure"))
    failure_message = _safe_text(failure.get("message"))
    if failure_message:
        return failure_message

    evaluation = _as_mapping(payload.get("evaluation"))
    reasons = [
        _safe_text(item)
        for item in _as_list(evaluation.get("reasons"))
        if _safe_text(item)
    ]
    if reasons:
        return reasons[0]

    stderr_clean = _normalise_prompt_sampler_error_text(stderr_text)
    if stderr_clean:
        return stderr_clean
    return fallback


def _run_prompt_sampler_case(
    *,
    case: Mapping[str, Any],
    base_url: str,
    timeout_seconds: float,
    model: str,
    allow_premium_model: bool,
    allow_non_agent_test_server: bool,
) -> dict[str, Any]:
    _enforce_local_only_policy(model, allow_premium_model)
    with tempfile.NamedTemporaryFile(prefix="replay_prompt_case_", suffix=".json", delete=False) as tmp_file:
        output_path = Path(tmp_file.name)

    command = [
        sys.executable,
        str(PROMPT_SAMPLER_SCRIPT_PATH),
        "--base-url",
        base_url,
        "--model",
        model,
        "--prompt-id",
        _safe_text(case.get("prompt_id")),
        "--timeout-seconds",
        str(max(timeout_seconds, 1.0)),
        "--output-json",
        str(output_path),
    ]
    if allow_non_agent_test_server:
        command.append("--allow-non-agent-test-server")
    if allow_premium_model:
        command.append("--allow-premium-model")

    process = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        timeout=max(timeout_seconds + 5.0, 5.0),
        cwd=str(PROJECT_ROOT),
    )
    payload = _read_json_file_if_present(output_path)
    if not payload:
        payload = _parse_json_from_stdout(process.stdout)

    if process.returncode != 0:
        reason = _extract_prompt_sampler_failure_reason(
            payload,
            stderr_text=process.stderr,
            fallback="prompt replay failed",
        )
        return {
            "result": "failed",
            "health": "0/1 (0%)",
            "failure_stall_reason": reason,
            "evidence": _extract_evidence_tokens(payload) or [f"return_code={process.returncode}"],
            "raw_result": payload,
        }

    repeat = _as_mapping(payload.get("repeat"))
    success_count = int(repeat.get("successful_attempt_count") or 1)
    attempt_count = int(repeat.get("attempt_count") or 1)
    meets_threshold = bool(repeat.get("meets_minimum_success_rate", True))
    status = _safe_text(payload.get("status"))
    should_pass = status == "ok" and meets_threshold and success_count >= 1
    percent = int((100.0 * success_count / attempt_count)) if attempt_count > 0 else 0
    return {
        "result": "passed" if should_pass else "failed",
        "health": f"{success_count}/{attempt_count} ({percent}%)",
        "failure_stall_reason": ""
        if should_pass
        else _extract_prompt_sampler_failure_reason(
            payload,
            stderr_text=process.stderr,
            fallback="Replay summary reported failure",
        ),
        "evidence": _extract_evidence_tokens(payload),
        "raw_result": payload,
    }


def _run_arxiv_workflow_case(
    *,
    base_url: str,
    timeout_seconds: float,
    allow_non_agent_test_server: bool,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ARXIV_WORKFLOW_SCRIPT_PATH),
        "--mode",
        "workflow_api",
        "--base-url",
        base_url,
        "--timeout-seconds",
        str(max(timeout_seconds, 30.0)),
    ]
    if allow_non_agent_test_server:
        command.append("--allow-non-agent-test-server")

    process = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        timeout=max(timeout_seconds + 5.0, 35.0),
        cwd=str(PROJECT_ROOT),
    )
    payload = _parse_json_from_stdout(process.stdout)
    if not payload:
        payload = _parse_json_from_stdout(process.stderr)

    passed = _safe_text(payload.get("status")) == "passed" and process.returncode == 0
    reason = _safe_text(payload.get("error"))
    if process.returncode != 0 and not reason:
        reason = _safe_text(process.stderr) or "arXiv workflow replay failed"
    return {
        "result": "passed" if passed else "failed",
        "health": "1/1 (100%)" if passed else "0/1 (0%)",
        "failure_stall_reason": "" if passed else reason,
        "evidence": _extract_evidence_tokens(payload) or [f"return_code={process.returncode}"],
        "raw_result": payload,
    }


def execute_replay_case(
    *,
    case: Mapping[str, Any],
    base_url: str,
    timeout_seconds: float,
    dry_run: bool,
    model: str,
    allow_premium_model: bool,
    allow_non_agent_test_server: bool,
) -> dict[str, Any]:
    start = time.monotonic()
    replay_id = _safe_text(case.get("replay_id"))
    runnable = bool(case.get("runnable"))
    if not runnable:
        return {
            "replay_id": replay_id,
            "result": "not_runnable",
            "health": "n/a",
            "failure_stall_reason": _safe_text(case.get("not_runnable_reason"))
            or "No executable harness mapped for this discovered replay definition.",
            "evidence": [],
            "duration_seconds": round(time.monotonic() - start, 3),
        }
    if dry_run:
        return {
            "replay_id": replay_id,
            "result": "skipped",
            "health": "n/a",
            "failure_stall_reason": "Dry run requested; replay execution skipped.",
            "evidence": [],
            "duration_seconds": round(time.monotonic() - start, 3),
        }
    try:
        execution_kind = _safe_text(case.get("execution_kind"))
        if execution_kind == "prompt_sampler":
            run_result = _run_prompt_sampler_case(
                case=case,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                model=model,
                allow_premium_model=allow_premium_model,
                allow_non_agent_test_server=allow_non_agent_test_server,
            )
        elif execution_kind == "arxiv_workflow_api":
            run_result = _run_arxiv_workflow_case(
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                allow_non_agent_test_server=allow_non_agent_test_server,
            )
        else:
            run_result = {
                "result": "not_runnable",
                "health": "n/a",
                "failure_stall_reason": "No execution adapter implemented for this replay case.",
                "evidence": [],
            }
    except subprocess.TimeoutExpired as exc:
        run_result = {
            "result": "timed_out",
            "health": "0/1 (0%)",
            "failure_stall_reason": f"Replay exceeded timeout after {timeout_seconds:.1f}s: {exc}",
            "evidence": [f"timeout_seconds={timeout_seconds:.1f}"],
        }
    except Exception as exc:  # pragma: no cover - defensive
        run_result = {
            "result": "failed",
            "health": "0/1 (0%)",
            "failure_stall_reason": str(exc),
            "evidence": [],
        }

    enriched = {
        "replay_id": replay_id,
        "result": _safe_text(run_result.get("result")) or "unknown",
        "health": _safe_text(run_result.get("health")) or "n/a",
        "failure_stall_reason": _safe_text(run_result.get("failure_stall_reason")),
        "evidence": [
            _safe_text(item)
            for item in _as_list(run_result.get("evidence"))
            if _safe_text(item)
        ],
        "duration_seconds": round(time.monotonic() - start, 3),
    }
    if isinstance(run_result.get("raw_result"), Mapping):
        enriched["raw_result"] = _as_mapping(run_result.get("raw_result"))
    return enriched


def render_markdown_table(rows: Sequence[Mapping[str, Any]]) -> str:
    headers = [
        "Replay ID",
        "Source",
        "What it tests",
        "Surface exercised",
        "Required tools/workflows",
        "Mode/environment",
        "Result",
        "Health",
        "Failure/stall reason",
        "Evidence",
    ]

    def esc(value: Any) -> str:
        return _safe_text(value).replace("|", "\\|").replace("\n", " ")

    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        output.append(
            "| "
            + " | ".join(
                [
                    esc(row.get("replay_id")),
                    esc(row.get("source")),
                    esc(row.get("what_it_tests")),
                    esc(row.get("surface_exercised")),
                    esc(row.get("required_tools_workflows")),
                    esc(row.get("mode_environment")),
                    esc(row.get("result")),
                    esc(row.get("health")),
                    esc(row.get("failure_stall_reason")),
                    esc(row.get("evidence")),
                ]
            )
            + " |"
        )
    return "\n".join(output)


def _build_table_row(case: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    additional_sources = [
        _safe_text(item)
        for item in _as_list(case.get("additional_sources"))
        if _safe_text(item)
    ]
    source_text = _safe_text(case.get("source"))
    if additional_sources:
        source_text = f"{source_text} (+{len(additional_sources)} sources)"

    required_items = []
    required_items.extend(
        _safe_text(item)
        for item in _as_list(case.get("required_tools"))
        if _safe_text(item)
    )
    required_items.extend(
        _safe_text(item)
        for item in _as_list(case.get("required_workflows"))
        if _safe_text(item)
    )
    required_items.extend(
        _safe_text(item)
        for item in _as_list(case.get("required_concepts"))
        if _safe_text(item)
    )
    dedup_required = sorted(set(required_items))
    evidence_text = "; ".join(
        _safe_text(item) for item in _as_list(result.get("evidence")) if _safe_text(item)
    )
    return {
        "replay_id": _safe_text(case.get("replay_id")),
        "source": source_text,
        "what_it_tests": _safe_text(case.get("what_it_tests")),
        "surface_exercised": _safe_text(case.get("surface_exercised")),
        "required_tools_workflows": ", ".join(dedup_required),
        "mode_environment": _safe_text(case.get("mode_environment")),
        "result": _safe_text(result.get("result")) or "unknown",
        "health": _safe_text(result.get("health")) or "n/a",
        "failure_stall_reason": _safe_text(result.get("failure_stall_reason")),
        "evidence": evidence_text,
    }


def run_replay_suite(
    cases: Sequence[Mapping[str, Any]],
    *,
    base_url: str,
    per_replay_timeout_seconds: float,
    suite_timeout_seconds: float,
    dry_run: bool,
    model: str,
    allow_premium_model: bool,
    allow_non_agent_test_server: bool,
) -> list[dict[str, Any]]:
    started = time.monotonic()
    deadline = started + max(float(suite_timeout_seconds), 1.0)
    results: list[dict[str, Any]] = []
    for case in cases:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append(
                {
                    "replay_id": _safe_text(case.get("replay_id")),
                    "result": "timed_out",
                    "health": "0/1 (0%)",
                    "failure_stall_reason": "Suite-level timeout exhausted before this replay could start.",
                    "evidence": [
                        f"suite_timeout_seconds={suite_timeout_seconds:.1f}",
                    ],
                    "duration_seconds": 0.0,
                }
            )
            continue
        case_timeout = min(max(float(per_replay_timeout_seconds), 1.0), remaining)
        case_result = execute_replay_case(
            case=case,
            base_url=base_url,
            timeout_seconds=case_timeout,
            dry_run=dry_run,
            model=model,
            allow_premium_model=allow_premium_model,
            allow_non_agent_test_server=allow_non_agent_test_server,
        )
        results.append(case_result)
    return results


def _count_results(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        status = _safe_text(result.get("result")) or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _write_text(path_text: str, content: str) -> None:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path_text: str, payload: Mapping[str, Any]) -> None:
    _write_text(path_text, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run discovered replay cases and emit a unified health report.",
    )
    parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        choices=REPLAY_SOURCE_CHOICES,
        default=[],
        help="Replay source/category filter; repeat flag to combine categories.",
    )
    parser.add_argument(
        "--replay-id",
        dest="replay_ids",
        action="append",
        default=[],
        help="Filter to one or more concrete replay IDs.",
    )
    parser.add_argument("--prompt-id", dest="prompt_ids", action="append", default=[])
    parser.add_argument(
        "--prompt-complexity-class",
        dest="prompt_complexity_classes",
        action="append",
        default=[],
        help="Optional prompt-bank complexity-class filter.",
    )
    parser.add_argument("--max-prompt-cases", type=int, default=None)
    parser.add_argument("--list", action="store_true", help="List discovered replay cases only.")
    parser.add_argument("--dry-run", action="store_true", help="Discover and classify without executing replay subprocesses.")
    parser.add_argument("--base-url", default=get_default_agent_test_base_url())
    parser.add_argument(
        "--allow-non-agent-test-server",
        action="store_true",
        help="Allow targeting non-AgentTest servers; otherwise downstream harnesses enforce AgentTest markers.",
    )
    parser.add_argument("--model", default=DEFAULT_PROMPT_MODEL)
    parser.add_argument(
        "--allow-premium-model",
        action="store_true",
        help="Permit premium model names for prompt-bank replay runs.",
    )
    parser.add_argument("--per-replay-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--suite-timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-markdown", default="")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    source_filters = {
        _safe_text(item)
        for item in _as_list(args.sources)
        if _safe_text(item)
    }
    replay_ids = {
        _safe_text(item)
        for item in _as_list(args.replay_ids)
        if _safe_text(item)
    }
    prompt_ids = {
        _safe_text(item)
        for item in _as_list(args.prompt_ids)
        if _safe_text(item)
    }
    prompt_complexity_classes = {
        _safe_text(item)
        for item in _as_list(args.prompt_complexity_classes)
        if _safe_text(item)
    }

    all_cases = discover_replay_cases(
        prompt_complexity_classes=prompt_complexity_classes or None,
        prompt_ids=prompt_ids or None,
        max_prompt_cases=args.max_prompt_cases,
    )
    selected_cases = filter_replay_cases(
        all_cases,
        source_filters=source_filters,
        replay_ids=replay_ids or None,
    )

    dry_run = bool(args.dry_run or args.list)
    if not dry_run:
        _enforce_local_only_policy(_safe_text(args.model), bool(args.allow_premium_model))

    results = run_replay_suite(
        selected_cases,
        base_url=_safe_text(args.base_url),
        per_replay_timeout_seconds=float(args.per_replay_timeout_seconds),
        suite_timeout_seconds=float(args.suite_timeout_seconds),
        dry_run=dry_run,
        model=_safe_text(args.model) or DEFAULT_PROMPT_MODEL,
        allow_premium_model=bool(args.allow_premium_model),
        allow_non_agent_test_server=bool(args.allow_non_agent_test_server),
    )

    rows = [
        _build_table_row(case, result)
        for case, result in zip(selected_cases, results, strict=False)
    ]
    markdown_report = render_markdown_table(rows)
    result_counts = _count_results(results)
    report_payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "source": sorted(source_filters),
            "replay_ids": sorted(replay_ids),
            "prompt_ids": sorted(prompt_ids),
            "prompt_complexity_classes": sorted(prompt_complexity_classes),
        },
        "execution_policy": {
            "dry_run": dry_run,
            "base_url": _safe_text(args.base_url),
            "allow_non_agent_test_server": bool(args.allow_non_agent_test_server),
            "model": _safe_text(args.model),
            "allow_premium_model": bool(args.allow_premium_model),
            "per_replay_timeout_seconds": float(args.per_replay_timeout_seconds),
            "suite_timeout_seconds": float(args.suite_timeout_seconds),
        },
        "counts": {
            "discovered": len(all_cases),
            "selected": len(selected_cases),
            "results": result_counts,
        },
        "cases": selected_cases,
        "results": results,
        "table_rows": rows,
    }

    print(markdown_report)
    if _safe_text(args.output_markdown):
        _write_text(_safe_text(args.output_markdown), markdown_report + "\n")
    if _safe_text(args.output_json):
        _write_json(_safe_text(args.output_json), report_payload)

    failing_statuses = {"failed", "timed_out"}
    has_failures = any(
        _safe_text(item.get("result")) in failing_statuses for item in results
    )
    return 1 if has_failures and not dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
