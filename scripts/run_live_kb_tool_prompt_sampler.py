"""Collect one sampled KB+tool-sensitive turn from a live Von server.

This script exercises the real `/von/generate` route in a fresh test
conversation, fetches persisted turn telemetry, and records what happened.
It deliberately does not implement a second semantic policy or evaluator in
Python.  A completed collection is not a claim that the answer was useful.

Use `--complexity-class` to constrain random selection to easier direct
questions, KB-grounded questions, or harder tool-augmented questions.

The prompt bank lives in `scripts/live_kb_tool_prompt_bank.json`. Runtime
selection loads that file so replay cases remain data artefacts rather than
task-specific Python policy.

Use this sampler together with
`docs/engineering/real_path_server_replay_and_telemetry_loop.md`.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import random
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.live_test_server_defaults import (
    DEFAULT_AGENT_TEST_BASE_URL,
    AGENT_TEST_BASE_URL_ENV_VAR,
    build_agent_test_server_requirement_error,
    get_default_agent_test_base_url,
    resolve_live_test_base_url,
)
from src.backend.services import (
    replay_arm_planning_service,
    replay_experiment_observation_service,
)
from src.backend.services.tool_observation_ledger_service import (
    TOOL_OBSERVATION_LEDGER_SCHEMA_VERSION,
    build_tool_observation_ledger,
)

DEFAULT_MODEL = "gemma4:31b"
DEFAULT_MINIMUM_COLLECTION_RATE = 0.95
DEFAULT_REPLAY_SET_ID = "JVNAUTOSCI-1894"
DEFAULT_USER_CONCEPT_ID = "#V#michael_witbrock"
DEFAULT_ORGANISATION_CONCEPT_ID = "university_of_auckland_strong_ai_lab"
DEFAULT_SESSION_NAME = "JVNAUTOSCI-1894 live prompt sample"
ACTIVE_AUTHENTICATED_MODEL_LABEL = "active_authenticated_model"
LOCAL_MODEL_PROVIDER_NAME = "ollama"
KNOWN_MODEL_PROVIDER_NAMES = frozenset(
    {LOCAL_MODEL_PROVIDER_NAME, "openai", "anthropic", "gemini", "azure_openai"}
)
OPENAI_MODEL_PREFIXES = (
    "gpt-",
    "gpt4",
    "gpt5",
    "o1",
    "o3",
    "o4",
    "text-davinci",
)
CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST = "coding_agent_test"
CHAT_SESSION_CREATED_BY_ACTOR_CONCEPT_ID = "#V#von_system"
CHAT_SESSION_CREATED_BY_ACTOR_TYPE = "#V#coding_agent"
LIVE_PROMPT_SAMPLER_TEST_ARTIFACT_KIND = "live_kb_tool_prompt_sampler_chat_session"
PROMPT_BANK_PATH = Path(__file__).with_name("live_kb_tool_prompt_bank.json")
REAL_PATH_REPLAY_GUIDE = (
    "docs/engineering/real_path_server_replay_and_telemetry_loop.md"
)
REAL_PATH_REPLAY_GUIDE_NOTE = (
    "Use this random prompt sampler together with "
    f"`{REAL_PATH_REPLAY_GUIDE}`. Before judging the turn, follow that guide's "
    "expectation-first preflight, real-path replay, false-empty verification, "
    "telemetry review, and thinking-panel review steps."
)
SERVER_METADATA_TIMEOUT_SECONDS = 15.0
ACTIVE_LLM_INFO_TIMEOUT_SECONDS = 15.0
PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS: dict[str, str] = {
    "direct_context_or_background": (
        "Questions that a capable direct-response LLM should usually answer from "
        "turn context, runtime context, or background knowledge without needing "
        "KB grounding or extra tools."
    ),
    "vontology_grounded": (
        "Questions that should build on direct/context knowledge plus represented "
        "Vontology or KB content, but do not inherently need live external tool "
        "surfaces such as web, Jira, or arXiv."
    ),
    "vontology_plus_single_tool": (
        "Questions that build on represented Vontology or KB context and invite "
        "one additional live retrieval or operational tool surface such as "
        "arXiv, Jira, or RAG, without broader multi-tool orchestration."
    ),
    "tool_augmented": (
        "Questions that combine direct/context and KB reasoning with live tool "
        "use, especially web, Jira, arXiv, or other external/operational surfaces."
    ),
}


def _load_prompt_bank_payload_from_file() -> dict[str, Any]:
    payload = json.loads(PROMPT_BANK_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Prompt bank file must contain a JSON object.")
    return payload


PROMPT_BANK_PAYLOAD = _load_prompt_bank_payload_from_file()


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


def _dedupe_texts(values: Sequence[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _safe_text(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def _split_model_provider_prefix(value: Any) -> tuple[str | None, str]:
    cleaned = _safe_text(value)
    if not cleaned or ":" not in cleaned:
        return None, cleaned
    provider, remainder = cleaned.split(":", 1)
    provider_key = provider.strip().lower()
    if provider_key in KNOWN_MODEL_PROVIDER_NAMES:
        return provider_key, remainder.strip()
    return None, cleaned


def _infer_provider_from_model_identifier(value: Any) -> str | None:
    provider, bare_model = _split_model_provider_prefix(value)
    if provider:
        return provider
    lowered = bare_model.lower()
    if not lowered:
        return None
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "gemini"
    if any(lowered.startswith(prefix) for prefix in OPENAI_MODEL_PREFIXES):
        return "openai"
    if ":" in bare_model and not lowered.startswith("ft:"):
        return LOCAL_MODEL_PROVIDER_NAME
    return None


def _load_json_mapping_argument(value: str, *, argument_name: str) -> dict[str, Any]:
    cleaned = _safe_text(value)
    if not cleaned:
        return {}
    raw_text = cleaned
    if cleaned.startswith("@"):
        raw_text = Path(cleaned[1:]).read_text(encoding="utf-8")
    elif cleaned.startswith("{"):
        raw_text = cleaned
    else:
        candidate_path = Path(cleaned)
        try:
            path_exists = candidate_path.exists() and candidate_path.is_file()
        except OSError:
            path_exists = False
        if path_exists:
            raw_text = candidate_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{argument_name} must be JSON or @path to JSON: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{argument_name} must decode to a JSON object.")
    return {str(key): item for key, item in payload.items() if isinstance(key, str)}


def _normalise_prompt_entry(
    *,
    prompt_text: str,
    replay_case_id: str,
    category: str,
    likely_tools: Sequence[Any] = (),
    knowledge_surfaces: Sequence[Any] = (),
    requires_tool_use: bool | None = None,
    source_kind: str | None = None,
    source_request_id: str | None = None,
    source_workflow_id: str | None = None,
) -> dict[str, Any]:
    cleaned_prompt = _safe_text(prompt_text)
    _assert(bool(cleaned_prompt), "Replay prompt text cannot be empty.")
    tool_names = _dedupe_texts(likely_tools)
    surfaces = _dedupe_texts(knowledge_surfaces)
    return {
        "id": _safe_text(replay_case_id) or f"ad_hoc_replay_{uuid.uuid4().hex[:12]}",
        "category": _safe_text(category) or "ad_hoc_replay",
        "complexity_class": "tool_augmented" if tool_names else "vontology_grounded",
        "prompt": cleaned_prompt,
        "knowledge_surfaces": surfaces or ["turn_context"],
        "likely_tools": tool_names,
        "requires_tool_use": (
            bool(tool_names) if requires_tool_use is None else bool(requires_tool_use)
        ),
        "source_kind": _safe_text(source_kind) or None,
        "source_request_id": _safe_text(source_request_id) or None,
        "source_workflow_id": _safe_text(source_workflow_id) or None,
    }


def _build_prompt_entry_from_failure_case(
    failure_case: Mapping[str, Any],
    *,
    replay_case_id: str | None,
) -> dict[str, Any]:
    turn = _as_mapping(failure_case.get("turn"))
    prompt_payload = _as_mapping(turn.get("prompt"))
    prompt_text = _safe_text(prompt_payload.get("text"))
    workflow = _as_mapping(failure_case.get("workflow"))
    tool_ledger = _as_mapping(failure_case.get("tool_ledger"))
    by_tool = [
        _safe_text(_as_mapping(item).get("tool"))
        for item in _as_list(tool_ledger.get("by_tool"))
    ]
    required_tools = [
        _safe_text(item) for item in _as_list(tool_ledger.get("required_tools"))
    ]
    request_id = _safe_text(failure_case.get("request_id"))
    case_id = _safe_text(replay_case_id) or request_id or "failure_case_replay"
    return _normalise_prompt_entry(
        prompt_text=prompt_text,
        replay_case_id=case_id,
        category="failure_case_replay",
        likely_tools=[*by_tool, *required_tools],
        knowledge_surfaces=["conversation_history", "turn_execution_diagnostics"],
        requires_tool_use=bool(by_tool or required_tools),
        source_kind="failure_case_intake",
        source_request_id=request_id or None,
        source_workflow_id=_safe_text(workflow.get("selected_workflow_id")) or None,
    )


def _collect_failure_case_prompt_entry(
    *,
    conversation_ref: Mapping[str, Any] | None,
    chat_history_lookup: Mapping[str, Any] | None,
    request_id: str | None,
    session_id: str | None,
    namespace: str | None,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    target_model: str | None,
    comparator_model: str | None,
    workflow_id: str | None,
    stage_id: str | None,
    current_request_id: str | None,
    reference_mode: str | None,
    reference_phrase: str | None,
    include_legacy: bool | None,
    history_tail_limit: int | None,
    replay_case_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from src.backend.services.failure_case_intake_service import (
        collect_failure_case_intake,
    )

    failure_case = collect_failure_case_intake(
        conversation_ref=conversation_ref,
        chat_history_lookup=chat_history_lookup,
        request_id=request_id,
        session_id=session_id,
        namespace=namespace,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        target_model=target_model,
        comparator_model=comparator_model,
        workflow_id=workflow_id,
        stage_id=stage_id,
        current_request_id=current_request_id,
        reference_mode=reference_mode,
        reference_phrase=reference_phrase,
        include_legacy=include_legacy,
        history_tail_limit=history_tail_limit,
    )
    if failure_case.get("success") is False:
        raise RuntimeError(
            "Failure-case intake did not produce a replayable prompt: "
            f"{json.dumps(failure_case, ensure_ascii=True, sort_keys=True)[:1200]}"
        )
    return (
        _build_prompt_entry_from_failure_case(
            failure_case,
            replay_case_id=replay_case_id,
        ),
        dict(failure_case),
    )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class BackgroundGenerateTaskError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        request_id: str | None = None,
        status_payload: Mapping[str, Any] | None = None,
        cancellation_payload: Mapping[str, Any] | None = None,
        timeout_reconciliation: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.request_id = request_id
        self.status_payload = dict(status_payload or {})
        self.cancellation_payload = dict(cancellation_payload or {})
        self.timeout_reconciliation = dict(timeout_reconciliation or {})

    def to_report(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "request_id": self.request_id,
            "status_payload": self.status_payload,
            "cancellation_payload": self.cancellation_payload,
            "timeout_reconciliation": self.timeout_reconciliation,
        }


class BackgroundGenerateTaskPending(RuntimeError):
    """The harness stopped observing a task that remains live on the server."""

    def __init__(
        self,
        message: str,
        *,
        task_id: str,
        request_id: str,
        status_payload: Mapping[str, Any] | None = None,
        timeout_reconciliation: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.request_id = request_id
        self.status_payload = dict(status_payload or {})
        self.timeout_reconciliation = dict(timeout_reconciliation or {})

    def to_report(self) -> dict[str, Any]:
        return {
            "schema_version": "background_generate_task_pending.v1",
            "task_id": self.task_id,
            "request_id": self.request_id,
            "status_payload": self.status_payload,
            "timeout_reconciliation": self.timeout_reconciliation,
            "status_endpoint": f"/von/api/task/status/{self.task_id}",
            "result_endpoint": f"/von/api/task/result/{self.task_id}",
            "task_left_running": True,
        }


def _build_replay_session_creation_payload(session_name: str) -> dict[str, Any]:
    # Mirrors the create_chat_session provenance contract without importing backend services.
    return {
        "session_name": session_name,
        "origin_kind": CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST,
        "created_by_actor_concept_id": CHAT_SESSION_CREATED_BY_ACTOR_CONCEPT_ID,
        "created_by_actor_type": CHAT_SESSION_CREATED_BY_ACTOR_TYPE,
        "is_agent_created": True,
        "test_artifact_kind": LIVE_PROMPT_SAMPLER_TEST_ARTIFACT_KIND,
    }


def _emit_replay_guide_note() -> None:
    print(
        f"NOTE: {REAL_PATH_REPLAY_GUIDE_NOTE}",
        file=sys.stderr,
    )


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    expected_status: int = 200,
    timeout_seconds: float = 120.0,
    **kwargs: Any,
) -> dict[str, Any]:
    response = session.request(
        str(method or "GET").upper(),
        url,
        timeout=max(float(timeout_seconds), 1.0),
        **kwargs,
    )
    try:
        payload = response.json()
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            f"{method} {url} returned non-JSON payload (status={response.status_code}): "
            f"{response.text[:500]}"
        ) from exc
    if response.status_code != expected_status:
        raise RuntimeError(
            f"{method} {url} failed with status {response.status_code}: "
            f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
        )
    return payload


def _load_prompt_bank() -> dict[str, Any]:
    payload = _load_prompt_bank_payload_from_file()
    if payload != PROMPT_BANK_PAYLOAD:
        raise RuntimeError(
            "Prompt bank file changed after the sampler module was imported. "
            "Re-run the sampler so replay selection uses a single prompt-bank snapshot."
        )
    return json.loads(json.dumps(payload))


def _write_json_output(output_json: str, payload: Mapping[str, Any]) -> None:
    output_path = Path(output_json)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git_capture(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _collect_run_environment(
    *,
    base_url: str,
    requested_model: str | None,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
) -> dict[str, Any]:
    return {
        "base_url": base_url,
        "requested_model": _safe_text(requested_model) or None,
        "authenticated_user_concept_id": _safe_text(user_concept_id) or None,
        "authenticated_organisation_concept_id": (
            _safe_text(organisation_concept_id) or None
        ),
        "session_name": _safe_text(session_name) or None,
        "run_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "local_python_executable": sys.executable,
        "local_python_version": platform.python_version(),
        "local_platform": platform.platform(),
        "local_working_directory": str(Path.cwd()),
        "local_repo_git_branch": _git_capture("rev-parse", "--abbrev-ref", "HEAD"),
        "local_repo_git_head": _git_capture("rev-parse", "HEAD"),
    }


def _summarise_server_diag(diag_payload: Mapping[str, Any]) -> dict[str, Any]:
    version_details = _as_mapping(diag_payload.get("version_details"))
    durable_startup = _as_mapping(diag_payload.get("durable_workflow_startup"))
    durable_workflows = _as_mapping(diag_payload.get("durable_workflows"))
    return {
        "server_reported_version": _safe_text(diag_payload.get("version")) or None,
        "server_reported_python_version": (
            _safe_text(diag_payload.get("python_version")) or None
        ),
        "server_reported_git_branch": (
            _safe_text(version_details.get("git_branch")) or None
        ),
        "server_reported_git_commit": (
            _safe_text(version_details.get("git_commit")) or None
        ),
        "server_reported_git_short_commit": (
            _safe_text(version_details.get("git_short_commit")) or None
        ),
        "server_reported_git_dirty": version_details.get("git_dirty"),
        "server_effective_user_concept_id": (
            _safe_text(diag_payload.get("effective_user_concept_id")) or None
        ),
        "server_header_user_concept_id": (
            _safe_text(diag_payload.get("header_user_concept_id")) or None
        ),
        "server_session_user_concept_id": (
            _safe_text(diag_payload.get("session_user_concept_id")) or None
        ),
        "server_durable_workflow_ready": durable_startup.get("ready"),
        "server_worker_running": durable_workflows.get("worker_running"),
        "server_scheduler_running": durable_workflows.get("scheduler_running"),
        "server_uptime_sec": diag_payload.get("uptime_sec"),
        "server_agent_test_instance": diag_payload.get("agent_test_instance"),
    }


def _augment_run_environment_with_server_diag(
    *,
    session: requests.Session,
    base_url: str,
    run_environment: dict[str, Any],
) -> dict[str, Any]:
    augmented_environment = dict(run_environment)
    last_error: str | None = None
    for source, path in (("health", "/health"), ("diag", "/diag")):
        try:
            payload = _request_json(
                session,
                "GET",
                f"{base_url}{path}",
                timeout_seconds=SERVER_METADATA_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            last_error = f"{source}: {exc}"
            continue
        augmented_environment.update(_summarise_server_diag(payload))
        augmented_environment["server_metadata_source"] = source
        augmented_environment["server_metadata_error"] = None
        return augmented_environment
    augmented_environment["server_metadata_source"] = None
    augmented_environment["server_metadata_error"] = last_error
    return augmented_environment


def _summarise_active_llm_info(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "server_resolved_active_llm_provider": (
            _safe_text(payload.get("provider")) or None
        ),
        "server_resolved_active_llm_model": _safe_text(payload.get("model")) or None,
        "server_resolved_active_llm_status": _safe_text(payload.get("status")) or None,
        "server_resolved_active_llm_ping_ok": payload.get("ping_ok"),
        "server_resolved_active_llm_error": _safe_text(payload.get("error")) or None,
    }


def _augment_run_environment_with_active_llm_info(
    *,
    session: requests.Session,
    base_url: str,
    user_concept_id: str,
    organisation_concept_id: str,
    run_environment: dict[str, Any],
) -> dict[str, Any]:
    augmented_environment = dict(run_environment)
    try:
        llm_info_payload = _request_json(
            session,
            "GET",
            f"{base_url}/api/settings/llm/info",
            timeout_seconds=ACTIVE_LLM_INFO_TIMEOUT_SECONDS,
            params={
                "user_concept_id": user_concept_id,
                "organisation_concept_id": organisation_concept_id,
            },
        )
    except Exception as exc:
        augmented_environment["server_resolved_active_llm_lookup_error"] = str(exc)
        return augmented_environment
    augmented_environment.update(_summarise_active_llm_info(llm_info_payload))
    augmented_environment["server_resolved_active_llm_lookup_error"] = None
    return augmented_environment


def _choose_prompt(
    prompt_bank: Sequence[Mapping[str, Any]],
    *,
    seed: int | None,
    prompt_id: str | None,
    allowed_complexity_classes: frozenset[str],
) -> dict[str, Any]:
    prompts = [
        dict(entry)
        for entry in prompt_bank
        if isinstance(entry, Mapping)
        and (
            not allowed_complexity_classes
            or _safe_text(entry.get("complexity_class")) in allowed_complexity_classes
        )
    ]
    _assert(
        bool(prompts),
        "Prompt bank is empty for the requested complexity-class filter.",
    )
    if isinstance(prompt_id, str) and prompt_id.strip():
        wanted = prompt_id.strip()
        for entry in prompts:
            if _safe_text(entry.get("id")) == wanted:
                return entry
        raise RuntimeError(f"Unknown prompt id: {wanted}")
    rng = random.Random(seed)
    return dict(rng.choice(prompts))


def _establish_authenticated_session(
    *,
    session: requests.Session,
    base_url: str,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
) -> tuple[str, str]:
    window_session_id = str(uuid.uuid4())
    session.headers.update(
        {
            "X-User-Concept-ID": user_concept_id,
            "X-Von-Window-Session": window_session_id,
        }
    )
    session_context_established = False
    try:
        _request_json(
            session,
            "POST",
            f"{base_url}/von/api/session/set_user_concept",
            json={"user_concept_id": user_concept_id},
        )
        session_context_established = True
    except RuntimeError:
        diag_payload = _request_json(session, "GET", f"{base_url}/diag")
        _assert(
            _safe_text(diag_payload.get("effective_user_concept_id"))
            == user_concept_id,
            f"Header-authenticated identity was not accepted: {diag_payload!r}",
        )

    if session_context_established and organisation_concept_id:
        _request_json(
            session,
            "POST",
            f"{base_url}/von/api/session/set_organisation",
            json={"organisation_concept_id": organisation_concept_id},
        )
    if session_context_established:
        context_payload = _request_json(
            session, "GET", f"{base_url}/von/api/session/context"
        )
        _assert(
            _safe_text(context_payload.get("user_id")) == user_concept_id,
            f"Session context user mismatch: {context_payload!r}",
        )

    session_payload = _request_json(
        session,
        "POST",
        f"{base_url}/von/api/session/create_chat_session",
        json=_build_replay_session_creation_payload(session_name),
    )
    session_id = _safe_text(session_payload.get("session_id"))
    _assert(bool(session_id), "Session creation did not return a session_id.")
    _request_json(session, "POST", f"{base_url}/von/reset", json={})
    return session_id, window_session_id


def _request_task_cancellation(
    *,
    session: requests.Session,
    base_url: str,
    task_id: str,
    await_terminal_seconds: float = 10.0,
    poll_interval_seconds: float = 0.5,
) -> dict[str, Any]:
    try:
        cancellation_payload = _request_json(
            session,
            "POST",
            f"{base_url}/von/api/task/cancel/{task_id}",
            timeout_seconds=15.0,
        )
    except Exception as exc:
        return {
            "success": False,
            "task_id": task_id,
            "error": str(exc),
        }

    result = dict(cancellation_payload)
    deadline = time.monotonic() + max(float(await_terminal_seconds), 0.0)
    last_status_payload: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        try:
            status_payload = _request_json(
                session,
                "GET",
                f"{base_url}/von/api/task/status/{task_id}",
                timeout_seconds=15.0,
            )
        except Exception as exc:
            result["post_cancellation_status_error"] = str(exc)
            break
        last_status_payload = status_payload
        status = _safe_text(status_payload.get("status"))
        if status in {"completed", "failed", "cancelled"}:
            result["post_cancellation_terminal"] = True
            result["post_cancellation_status_payload"] = status_payload
            return result
        if await_terminal_seconds <= 0:
            break
        time.sleep(max(float(poll_interval_seconds), 0.2))

    if last_status_payload is not None:
        result["post_cancellation_terminal"] = False
        result["post_cancellation_status_payload"] = last_status_payload
    return result


def _late_terminal_grace_seconds(
    *,
    poll_interval_seconds: float,
    override_seconds: float | None = None,
) -> float:
    if override_seconds is not None:
        return max(0.0, float(override_seconds))
    return min(15.0, max(2.0, float(poll_interval_seconds) * 5.0))


def _fetch_late_terminal_background_result(
    *,
    session: requests.Session,
    base_url: str,
    task_id: str,
    timeout_status_payload: Mapping[str, Any] | None,
    poll_interval_seconds: float,
    grace_seconds: float,
) -> dict[str, Any]:
    reconciliation: dict[str, Any] = {
        "schema_version": "background_task_observation_reconciliation.v1",
        "observer_window_expired": True,
        "late_terminal_result_observed": False,
        "task_id": task_id,
        "timeout_status_payload": dict(timeout_status_payload or {}),
        "grace_seconds": max(0.0, float(grace_seconds)),
    }
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    last_status_payload: dict[str, Any] | None = None
    while True:
        try:
            status_payload = _request_json(
                session,
                "GET",
                f"{base_url}/von/api/task/status/{task_id}",
                timeout_seconds=15.0,
            )
        except Exception as exc:
            reconciliation["status_readback_error"] = str(exc)
            break

        last_status_payload = dict(status_payload)
        status = _safe_text(status_payload.get("status"))
        if status in {"completed", "failed", "cancelled"}:
            reconciliation["final_status_payload"] = dict(status_payload)
            reconciliation["final_status"] = status
            if status == "completed":
                try:
                    task_result_payload = _request_json(
                        session,
                        "GET",
                        f"{base_url}/von/api/task/result/{task_id}",
                        timeout_seconds=30.0,
                    )
                except Exception as exc:
                    reconciliation["result_readback_error"] = str(exc)
                    break
                result_payload = _as_mapping(task_result_payload.get("result"))
                reconciliation["result_payload_observed"] = bool(result_payload)
                if result_payload:
                    reconciliation["late_terminal_result_observed"] = True
                    reconciliation["result_payload"] = dict(result_payload)
            break

        if time.monotonic() >= deadline:
            break
        time.sleep(max(float(poll_interval_seconds), 0.2))

    if last_status_payload is not None and "final_status_payload" not in reconciliation:
        reconciliation["final_status_payload"] = last_status_payload
        reconciliation["final_status"] = (
            _safe_text(last_status_payload.get("status")) or None
        )
    return reconciliation


def _run_generate_background(
    *,
    session: requests.Session,
    base_url: str,
    prompt: str,
    model: str | None,
    gmail_profile: str | None,
    presenter_mode: bool,
    timeout_seconds: float,
    poll_interval_seconds: float,
    include_status_payload: bool = False,
    late_terminal_grace_seconds: float | None = None,
    cancel_on_observer_expiry: bool = False,
) -> tuple[str, dict[str, Any]]:
    client_request_id = f"live-kb-prompt-{uuid.uuid4()}"
    request_payload: dict[str, Any] = {
        "prompt": prompt,
        "background": True,
        "client_request_id": client_request_id,
    }
    cleaned_model = _safe_text(model)
    if cleaned_model:
        request_payload["model"] = cleaned_model
    cleaned_gmail_profile = _safe_text(gmail_profile)
    if cleaned_gmail_profile:
        request_payload["gmail_profile"] = cleaned_gmail_profile
    if presenter_mode:
        request_payload["presenter_mode"] = True
    model_provider = _infer_provider_from_model_identifier(cleaned_model)
    if model_provider:
        request_payload["model_provider"] = model_provider
        request_payload["selected_model_provider"] = model_provider
    submission = _request_json(
        session,
        "POST",
        f"{base_url}/von/generate",
        expected_status=202,
        timeout_seconds=max(float(timeout_seconds), 30.0),
        json=request_payload,
    )
    task_id = _safe_text(submission.get("task_id"))
    _assert(
        bool(task_id), f"Background submission did not return task_id: {submission!r}"
    )
    request_id = _safe_text(submission.get("request_id")) or client_request_id

    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    status_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        status_payload = _request_json(
            session,
            "GET",
            f"{base_url}/von/api/task/status/{task_id}",
        )
        status = _safe_text(status_payload.get("status"))
        if status == "completed":
            break
        if status in {"failed", "cancelled"}:
            raise BackgroundGenerateTaskError(
                "Background generate task did not complete successfully: "
                f"{json.dumps(status_payload, ensure_ascii=True, sort_keys=True)}",
                task_id=task_id,
                request_id=request_id,
                status_payload=status_payload,
            )
        time.sleep(max(float(poll_interval_seconds), 0.2))

    if not (
        isinstance(status_payload, dict)
        and _safe_text(status_payload.get("status")) == "completed"
    ):
        timeout_reconciliation = _fetch_late_terminal_background_result(
            session=session,
            base_url=base_url,
            task_id=task_id,
            timeout_status_payload=status_payload or {},
            poll_interval_seconds=poll_interval_seconds,
            grace_seconds=_late_terminal_grace_seconds(
                poll_interval_seconds=poll_interval_seconds,
                override_seconds=late_terminal_grace_seconds,
            ),
        )
        if timeout_reconciliation.get("late_terminal_result_observed") is True:
            generate_payload = _as_mapping(timeout_reconciliation.get("result_payload"))
            if generate_payload:
                generate_payload = dict(generate_payload)
                generate_payload["background_task_timeout_reconciliation"] = {
                    key: value
                    for key, value in timeout_reconciliation.items()
                    if key != "result_payload"
                }
                final_status_payload = timeout_reconciliation.get(
                    "final_status_payload"
                )
                if isinstance(final_status_payload, Mapping):
                    generate_payload["background_task_status"] = dict(
                        final_status_payload
                    )
                return task_id, generate_payload
        final_status = _safe_text(timeout_reconciliation.get("final_status"))
        if final_status in {"failed", "cancelled"}:
            raise BackgroundGenerateTaskError(
                "Background generate task reached a non-success terminal state: "
                f"{json.dumps(timeout_reconciliation, ensure_ascii=True, sort_keys=True)}",
                task_id=task_id,
                request_id=request_id,
                status_payload=_as_mapping(
                    timeout_reconciliation.get("final_status_payload")
                ),
                timeout_reconciliation=timeout_reconciliation,
            )
        if cancel_on_observer_expiry:
            cancellation_payload = _request_task_cancellation(
                session=session,
                base_url=base_url,
                task_id=task_id,
                poll_interval_seconds=poll_interval_seconds,
            )
            raise BackgroundGenerateTaskError(
                "Background generate task was explicitly cancelled after the "
                "observer window elapsed: "
                f"{json.dumps(status_payload or {}, ensure_ascii=True, sort_keys=True)}",
                task_id=task_id,
                request_id=request_id,
                status_payload=status_payload or {},
                cancellation_payload=cancellation_payload,
                timeout_reconciliation=timeout_reconciliation,
            )
        timeout_reconciliation = {
            **timeout_reconciliation,
            "request_id": request_id,
            "status_endpoint": f"/von/api/task/status/{task_id}",
            "result_endpoint": f"/von/api/task/result/{task_id}",
            "task_left_running": True,
        }
        raise BackgroundGenerateTaskPending(
            "The replay observation window elapsed while the background task "
            "was still live. The task was not cancelled.",
            task_id=task_id,
            request_id=request_id,
            status_payload=status_payload or {},
            timeout_reconciliation=timeout_reconciliation,
        )
    task_result_payload = _request_json(
        session,
        "GET",
        f"{base_url}/von/api/task/result/{task_id}",
    )
    generate_payload = _as_mapping(task_result_payload.get("result"))
    diagnostic_settle_seconds = min(
        60.0,
        max(30.0, float(poll_interval_seconds) * 3.0),
    )
    diagnostic_settle_deadline = min(
        deadline, time.monotonic() + diagnostic_settle_seconds
    )
    while (
        isinstance(generate_payload.get("llm_debug"), Mapping)
        and not _as_mapping(
            _as_mapping(generate_payload.get("llm_debug")).get(
                "turn_execution_diagnostics"
            )
        )
        and time.monotonic() < diagnostic_settle_deadline
    ):
        time.sleep(max(float(poll_interval_seconds), 0.2))
        task_result_payload = _request_json(
            session,
            "GET",
            f"{base_url}/von/api/task/result/{task_id}",
        )
        generate_payload = _as_mapping(task_result_payload.get("result"))
    if not generate_payload:
        raise BackgroundGenerateTaskError(
            f"Background task result was empty: {task_result_payload!r}",
            task_id=task_id,
            request_id=request_id,
            status_payload=status_payload or {},
        )
    if include_status_payload and isinstance(status_payload, Mapping):
        generate_payload = dict(generate_payload)
        generate_payload["background_task_status"] = dict(status_payload)
    return task_id, generate_payload


def _extract_request_and_session_ids(
    *,
    task_id: str,
    generate_payload: Mapping[str, Any],
    default_session_id: str,
) -> tuple[str, str]:
    request_id = (
        _safe_text(generate_payload.get("request_id"))
        or _safe_text(_as_mapping(generate_payload.get("llm_debug")).get("request_id"))
        or task_id
    )
    session_id = (
        _safe_text(generate_payload.get("conversation_session_id"))
        or _safe_text(generate_payload.get("session_id"))
        or _safe_text(_as_mapping(generate_payload.get("llm_debug")).get("session_id"))
        or default_session_id
    )
    _assert(
        bool(session_id),
        f"Could not determine conversation session id: {generate_payload!r}",
    )
    return request_id, session_id


def _find_assistant_turn_history_location(
    *,
    session: requests.Session,
    base_url: str,
    session_id: str,
    request_id: str,
    response_text: str,
) -> dict[str, Any]:
    history_payload = _request_json(
        session,
        "GET",
        f"{base_url}/von/history",
        params={"session_id": session_id, "tail_limit": 8},
    )
    history = _as_list(history_payload.get("history"))
    target_response = _safe_text(response_text)
    for entry in reversed(history):
        if not isinstance(entry, Mapping):
            continue
        if _safe_text(entry.get("role")) != "assistant":
            continue
        debug_data = _as_mapping(entry.get("llm_debug_data"))
        debug_request_id = _safe_text(debug_data.get("request_id"))
        location = _as_mapping(entry.get("history_location"))
        if debug_request_id and debug_request_id == request_id and location:
            return location
        if (
            target_response
            and _safe_text(entry.get("content")) == target_response
            and location
        ):
            return location
    raise RuntimeError(
        f"Could not resolve assistant history location for request_id={request_id} "
        f"in session_id={session_id}."
    )


def _fetch_turn_debug(
    *,
    session: requests.Session,
    base_url: str,
    session_id: str,
    history_index: int,
) -> dict[str, Any]:
    debug_payload = _request_json(
        session,
        "GET",
        f"{base_url}/von/history/debug",
        params={"session_id": session_id, "history_index": history_index},
    )
    _assert(
        bool(debug_payload.get("success")),
        f"History debug lookup failed: {debug_payload!r}",
    )
    return _as_mapping(debug_payload.get("llm_debug_data"))


TASK_RESULT_DEBUG_COPY_KEYS = (
    "request_id",
    "session_id",
    "conversation_session_id",
    "model",
    "response",
    "response_text",
    "tool_invocations",
    "aux_llm_calls",
    "llm_calls",
    "workflow_discovery",
    "workflow_routing",
    "selected_workflow_trace",
    "turn_execution_diagnostics",
    "turn_execution_record",
    "completion_gate",
    "completion_gate_verdict",
    "completion_report",
    "required_tool_obligation_ledger",
    "render_plan",
    "turn_output_health",
    "warnings",
    "background_task_status",
    "background_task_timeout_reconciliation",
)

TASK_RESULT_DIAGNOSTIC_EVIDENCE_KEYS = frozenset(
    {
        "tool_invocations",
        "aux_llm_calls",
        "llm_calls",
        "workflow_discovery",
        "workflow_routing",
        "selected_workflow_trace",
        "turn_execution_diagnostics",
        "turn_execution_record",
        "completion_gate",
        "completion_gate_verdict",
        "completion_report",
        "required_tool_obligation_ledger",
        "render_plan",
        "turn_output_health",
    }
)


def _build_task_result_debug_payload(
    *,
    generate_payload: Mapping[str, Any],
    session_id: str,
    request_id: str,
    response_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    debug_payload = dict(_as_mapping(generate_payload.get("llm_debug")))
    copied_keys: list[str] = []
    for key in TASK_RESULT_DEBUG_COPY_KEYS:
        if key in debug_payload or key not in generate_payload:
            continue
        debug_payload[key] = generate_payload[key]
        copied_keys.append(key)

    if request_id and not _safe_text(debug_payload.get("request_id")):
        debug_payload["request_id"] = request_id
    if session_id and not (
        _safe_text(debug_payload.get("session_id"))
        or _safe_text(debug_payload.get("conversation_session_id"))
    ):
        debug_payload["session_id"] = session_id
    if response_text and not (
        _safe_text(debug_payload.get("response"))
        or _safe_text(debug_payload.get("response_text"))
    ):
        debug_payload["response"] = response_text

    diagnostic_keys = [
        key
        for key in sorted(TASK_RESULT_DIAGNOSTIC_EVIDENCE_KEYS)
        if key in debug_payload and debug_payload.get(key) not in (None, {}, [])
    ]
    has_response = bool(
        _safe_text(debug_payload.get("response"))
        or _safe_text(debug_payload.get("response_text"))
    )
    if not diagnostic_keys and not has_response:
        return {}, {}

    provenance = {
        "source": (
            "background_task_result.llm_debug"
            if isinstance(generate_payload.get("llm_debug"), Mapping)
            else "background_task_result"
        ),
        "copied_top_level_keys": copied_keys,
        "diagnostic_keys": diagnostic_keys,
        "has_turn_execution_diagnostics": bool(
            _as_mapping(debug_payload.get("turn_execution_diagnostics"))
        ),
        "partial_debug_payload": not bool(
            _as_mapping(debug_payload.get("turn_execution_diagnostics"))
        ),
    }
    debug_payload["replay_sampler_readback"] = dict(provenance)
    return debug_payload, provenance


def _debug_value_is_present(value: Any) -> bool:
    """Return whether a terminal debug value contains usable evidence."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, Sequence)) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return bool(value)
    return True


def _is_debug_blob_ref(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("schema_version") == "debug_payload_blob_ref.v1"
        and isinstance(value.get("blob_ref"), Mapping)
    )


def _merge_terminal_task_debug_value(history_value: Any, task_value: Any) -> Any:
    """Merge persisted history with the completed task's terminal debug value.

    History persistence can be observed just before the background task result
    receives its final telemetry enrichment.  The terminal task result is the
    authoritative same-request projection for overlapping populated fields,
    while history-only fields still need to survive.  Hydrated payloads also
    take precedence over opaque blob references in either direction.
    """

    if not _debug_value_is_present(task_value):
        return history_value
    if not _debug_value_is_present(history_value):
        return task_value
    if _is_debug_blob_ref(history_value) and not _is_debug_blob_ref(task_value):
        return task_value
    if _is_debug_blob_ref(task_value) and not _is_debug_blob_ref(history_value):
        return history_value
    if isinstance(history_value, Mapping) and isinstance(task_value, Mapping):
        merged = dict(history_value)
        for key, value in task_value.items():
            merged[key] = _merge_terminal_task_debug_value(merged.get(key), value)
        return merged
    return task_value


def _merge_history_and_task_result_debug(
    history_debug: Mapping[str, Any],
    task_result_debug: Mapping[str, Any],
) -> dict[str, Any]:
    """Preserve history-only evidence while applying terminal task enrichment."""

    merged = _merge_terminal_task_debug_value(history_debug, task_result_debug)
    return dict(merged) if isinstance(merged, Mapping) else dict(history_debug)


def _resolve_turn_debug_data(
    *,
    session: requests.Session,
    base_url: str,
    session_id: str,
    request_id: str,
    response_text: str,
    generate_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    task_result_debug, task_result_debug_provenance = _build_task_result_debug_payload(
        generate_payload=generate_payload,
        session_id=session_id,
        request_id=request_id,
        response_text=response_text,
    )
    try:
        history_location = _find_assistant_turn_history_location(
            session=session,
            base_url=base_url,
            session_id=session_id,
            request_id=request_id,
            response_text=response_text,
        )
        history_index_raw = history_location.get("history_index")
        if not isinstance(history_index_raw, int):
            raise RuntimeError(
                "History location did not include an integer history_index: "
                f"{history_location!r}"
            )
        history_debug = _fetch_turn_debug(
            session=session,
            base_url=base_url,
            session_id=session_id,
            history_index=history_index_raw,
        )
        if task_result_debug:
            history_debug = _merge_history_and_task_result_debug(
                history_debug,
                task_result_debug,
            )
        return (
            dict(history_location),
            history_debug,
        )
    except RuntimeError as exc:
        if task_result_debug:
            return (
                {
                    **dict(task_result_debug_provenance),
                    "session_id": session_id,
                    "request_id": request_id,
                    "history_lookup_error": str(exc),
                },
                dict(task_result_debug),
            )
        raise


def _collect_tool_names(
    diagnostics: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any],
) -> list[str]:
    tool_names: list[str] = []
    turn_record = _as_mapping(llm_debug_data.get("turn_execution_record"))
    for entries in (
        _as_list(diagnostics.get("tool_history")),
        _as_list(llm_debug_data.get("tool_invocations")),
        _as_list(turn_record.get("tool_invocations")),
    ):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            tool_name = _safe_text(entry.get("tool") or entry.get("method"))
            if tool_name and tool_name not in tool_names:
                tool_names.append(tool_name)
    return tool_names


def _extract_timing_metrics(
    diagnostics: Mapping[str, Any],
    turn_record: Mapping[str, Any],
) -> dict[str, Any]:
    timing = _as_mapping(diagnostics.get("timing_breakdown"))
    if not timing:
        timing = _as_mapping(turn_record.get("timing_breakdown"))
    totals = _as_mapping(timing.get("totals"))
    return {
        "elapsed_ms": totals.get("elapsed_ms"),
        "llm_elapsed_ms": totals.get("llm_elapsed_ms"),
        "llm_call_count": totals.get("llm_call_count"),
        "llm_calls_by_stage_model": _as_list(timing.get("llm_calls_by_stage_model")),
    }


def _record_experiment_observations(
    *,
    run_id: str,
    arm_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return replay_experiment_observation_service.record_experiment_observations(
        run_id=run_id,
        arm_summaries=arm_summaries,
        default_replay_set_id=DEFAULT_REPLAY_SET_ID,
    )


def _build_prompt_summary(prompt_entry: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        "id": _safe_text(prompt_entry.get("id")),
        "category": _safe_text(prompt_entry.get("category")),
        "complexity_class": _safe_text(prompt_entry.get("complexity_class")),
        "text": _safe_text(prompt_entry.get("prompt")),
        "knowledge_surfaces": _as_list(prompt_entry.get("knowledge_surfaces")),
        "likely_tools": _as_list(prompt_entry.get("likely_tools")),
        "requires_tool_use": bool(prompt_entry.get("requires_tool_use")),
        "allows_grounded_empty_result": bool(
            prompt_entry.get("allows_grounded_empty_result")
        ),
    }
    for source_key in ("source_kind", "source_request_id", "source_workflow_id"):
        source_value = _safe_text(prompt_entry.get(source_key))
        if source_value:
            summary[source_key] = source_value
    return summary


def _build_selection_summary(
    *,
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    requested_model: str | None,
    requested_model_arms: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    selection = {
        "prompt_bank_schema_version": prompt_bank_schema_version,
        "requested_complexity_classes": list(requested_complexity_classes),
        "seed": seed,
        "requested_model": _safe_text(requested_model) or None,
    }
    if requested_model_arms:
        selection["requested_model_arms"] = [
            {
                "arm_id": _safe_text(entry.get("arm_id")) or None,
                "label": _safe_text(entry.get("label")) or None,
                "requested_model": _safe_text(entry.get("requested_model")) or None,
                "requested_provider": (
                    _safe_text(entry.get("requested_provider")) or None
                ),
                **{
                    optional_key: optional_value
                    for optional_key in (
                        "model_arm_id",
                        "base_prompt_id",
                        "candidate_prompt_variant_id",
                        "workflow_stage_id",
                        "target_workflow_id",
                        "replay_set_id",
                        "replay_case_id",
                    )
                    if (optional_value := _safe_text(entry.get(optional_key)))
                },
            }
            for entry in requested_model_arms
            if isinstance(entry, Mapping)
        ]
    return selection


def _build_summary(
    *,
    prompt_entry: Mapping[str, Any],
    task_id: str,
    session_id: str,
    request_id: str,
    history_location: Mapping[str, Any],
    generate_payload: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    requested_model: str | None,
    run_environment: Mapping[str, Any],
    arm_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics = _as_mapping(llm_debug_data.get("turn_execution_diagnostics"))
    routing = _as_mapping(diagnostics.get("workflow_routing_diagnostics"))
    dispatch = _as_mapping(routing.get("dispatch"))
    turn_record = _as_mapping(llm_debug_data.get("turn_execution_record"))
    tool_history = _as_list(diagnostics.get("tool_history"))
    tool_observation_ledger = _as_mapping(llm_debug_data.get("tool_observation_ledger"))
    if not tool_observation_ledger:
        tool_observation_ledger = _as_mapping(
            diagnostics.get("tool_observation_ledger")
        )
    if not tool_observation_ledger:
        tool_observation_ledger = _as_mapping(
            turn_record.get("tool_observation_ledger")
        )
    if not tool_observation_ledger:
        tool_observation_ledger = _as_mapping(
            _as_mapping(turn_record.get("execution")).get("tool_observation_ledger")
        )
    if not tool_observation_ledger:
        derived_tool_observation_ledger = build_tool_observation_ledger(
            tool_invocations=[
                entry
                for entry in _as_list(
                    turn_record.get("tool_invocations")
                    or llm_debug_data.get("tool_invocations")
                )
                if isinstance(entry, Mapping)
            ],
            turn_execution_diagnostics=diagnostics,
            aux_llm_calls=[
                entry
                for entry in _as_list(llm_debug_data.get("aux_llm_calls"))
                if isinstance(entry, Mapping)
            ],
        )
        if int(derived_tool_observation_ledger.get("observation_count") or 0) > 0:
            tool_observation_ledger = derived_tool_observation_ledger
    observed_tools = _collect_tool_names(diagnostics, llm_debug_data)
    if not observed_tools:
        observed_tools = _dedupe_texts(
            _as_list(tool_observation_ledger.get("observed_tools"))
        )
    tool_invocations = [
        dict(entry)
        for entry in _as_list(
            turn_record.get("tool_invocations")
            or llm_debug_data.get("tool_invocations")
        )[:40]
        if isinstance(entry, Mapping)
    ]
    llm_calls = [
        dict(entry)
        for entry in _as_list(
            turn_record.get("llm_calls") or llm_debug_data.get("llm_calls")
        )[:40]
        if isinstance(entry, Mapping)
    ]
    response_text = (
        _safe_text(generate_payload.get("response"))
        or _safe_text(generate_payload.get("response_text"))
        or _safe_text(llm_debug_data.get("response"))
    )
    background_status = _safe_text(
        _as_mapping(generate_payload.get("background_task_status")).get("status")
        or generate_payload.get("background_task_status")
    )
    terminal_observations: dict[str, dict[str, Any]] = {}
    for source, container in (
        ("turn_execution_record", turn_record),
        ("task_result_debug", llm_debug_data),
    ):
        observed = {
            key: container[key]
            for key in (
                "completion_gate",
                "completion_gate_verdict",
                "terminal_outcome_receipt",
                "terminal_outcome_receipt_validation",
            )
            if _debug_value_is_present(container.get(key))
        }
        if observed:
            terminal_observations[source] = observed
    summary = {
        "status": "ok",
        "guidance": {
            "replay_guide_path": REAL_PATH_REPLAY_GUIDE,
            "replay_guide_note": REAL_PATH_REPLAY_GUIDE_NOTE,
        },
        "environment": dict(run_environment),
        "selection": _build_selection_summary(
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            requested_model=requested_model,
        ),
        "prompt": _build_prompt_summary(prompt_entry),
        "conversation": {
            "session_id": session_id,
            "request_id": request_id,
            "background_task_id": task_id,
            "history_location": dict(history_location),
        },
        "response": {
            "text": response_text,
            "background_task_status": (
                dict(_as_mapping(generate_payload.get("background_task_status")))
                or generate_payload.get("background_task_status")
            ),
            "background_task_timeout_reconciliation": (
                dict(
                    _as_mapping(
                        generate_payload.get("background_task_timeout_reconciliation")
                    )
                )
                or None
            ),
        },
        "telemetry": {
            "requested_model": _safe_text(requested_model) or None,
            "model": (
                _safe_text(turn_record.get("model"))
                or _safe_text(llm_debug_data.get("model"))
                or None
            ),
            "ordinary_turn_terminal_status": (
                _safe_text(turn_record.get("terminal_status"))
                or background_status
                or None
            ),
            "debug_readback_source": (
                _safe_text(history_location.get("source")) or "history_debug"
            ),
            "history_lookup_error": (
                _safe_text(history_location.get("history_lookup_error")) or None
            ),
            "debug_readback_partial": bool(
                history_location.get("partial_debug_payload")
            ),
            "debug_readback_has_turn_execution_diagnostics": bool(diagnostics),
            "selected_workflow_id": _safe_text(
                dispatch.get("dispatch_workflow_id")
                or routing.get("selected_workflow_id")
                or _as_mapping(llm_debug_data.get("workflow_routing")).get(
                    "workflow_id"
                )
            ),
            "selected_execution_mode": _safe_text(
                dispatch.get("selected_execution_mode")
            ),
            "dispatch_terminal_failure_reason": _safe_text(
                dispatch.get("dispatch_terminal_failure_reason")
            )
            or None,
            "dispatch_terminal_failure_detail": _safe_text(
                dispatch.get("dispatch_terminal_failure_detail")
            )
            or None,
            "tool_history": tool_history,
            "tool_invocations": tool_invocations,
            "tool_observation_ledger": (
                dict(tool_observation_ledger)
                if tool_observation_ledger.get("schema_version")
                == TOOL_OBSERVATION_LEDGER_SCHEMA_VERSION
                else None
            ),
            "observed_tools": observed_tools,
            "tool_count": (
                len(observed_tools)
                if observed_tools
                else len(tool_invocations) or len(tool_history)
            ),
            "workflow_routing_diagnostics": routing,
            "timing": _extract_timing_metrics(diagnostics, turn_record),
            "llm_calls": llm_calls,
            "terminal_observations": terminal_observations or None,
            "turn_execution_record": turn_record or None,
        },
    }
    if arm_metadata:
        summary["arm"] = {
            "arm_id": _safe_text(arm_metadata.get("arm_id")) or None,
            "label": _safe_text(arm_metadata.get("label")) or None,
            "requested_model": _safe_text(arm_metadata.get("requested_model")) or None,
            "requested_provider": (
                _safe_text(arm_metadata.get("requested_provider")) or None
            ),
        }
        for optional_key in (
            "model_arm_id",
            "base_prompt_id",
            "candidate_prompt_variant_id",
            "workflow_stage_id",
            "target_workflow_id",
            "replay_set_id",
            "replay_case_id",
        ):
            optional_value = _safe_text(arm_metadata.get(optional_key))
            if optional_value:
                summary["arm"][optional_key] = optional_value
        if _safe_text(arm_metadata.get("base_prompt_id")) or _safe_text(
            arm_metadata.get("candidate_prompt_variant_id")
        ):
            summary["prompt_variant_evaluation"] = (
                replay_experiment_observation_service.build_prompt_variant_evaluation(
                    llm_debug_data=llm_debug_data,
                    arm_metadata=arm_metadata,
                    requested_model=requested_model,
                )
            )
            summary["replay_scoring_consistency"] = (
                replay_experiment_observation_service.build_replay_scoring_consistency(
                    llm_debug_data=llm_debug_data,
                )
            )
    return summary


def _build_model_arm_plan(
    *,
    requested_model: str | None,
    compare_models: Sequence[str],
    include_active_model_arm: bool,
) -> list[dict[str, Any]]:
    planned_arms: list[dict[str, Any]] = []
    seen_models: set[str] = set()

    def _append_arm(model_name: str | None) -> None:
        cleaned_model = _safe_text(model_name) or None
        dedupe_key = cleaned_model or "__active_authenticated_model__"
        if dedupe_key in seen_models:
            return
        seen_models.add(dedupe_key)
        planned_arms.append(
            {
                "arm_id": f"arm_{len(planned_arms) + 1}",
                "label": cleaned_model or ACTIVE_AUTHENTICATED_MODEL_LABEL,
                "requested_model": cleaned_model,
                "requested_provider": (
                    _infer_provider_from_model_identifier(cleaned_model)
                    if cleaned_model
                    else None
                ),
            }
        )

    if include_active_model_arm:
        _append_arm(None)
    _append_arm(requested_model)
    for entry in compare_models:
        cleaned_entry = _safe_text(entry)
        if cleaned_entry:
            _append_arm(cleaned_entry)
    if not planned_arms:
        _append_arm(None)
    return planned_arms


def _build_replay_arm_plan(
    *,
    model_arms: Sequence[Mapping[str, Any]],
    base_prompt_id: str | None,
    prompt_variant_ids: Sequence[Any],
    workflow_stage_id: str | None,
    target_workflow_id: str | None,
    replay_set_id: str | None,
    replay_case_id: str | None,
) -> list[dict[str, Any]]:
    return replay_arm_planning_service.build_replay_arm_plan(
        model_arms=model_arms,
        base_prompt_id=base_prompt_id,
        prompt_variant_ids=prompt_variant_ids,
        workflow_stage_id=workflow_stage_id,
        target_workflow_id=target_workflow_id,
        replay_set_id=replay_set_id,
        replay_case_id=replay_case_id,
        default_replay_set_id=DEFAULT_REPLAY_SET_ID,
    )


def _replay_arms_require_active_llm_info(
    replay_arms: Sequence[Mapping[str, Any]],
) -> bool:
    return any(not _safe_text(arm.get("requested_model")) for arm in replay_arms)


def _build_arm_session_name(
    *, base_session_name: str, arm_metadata: Mapping[str, Any] | None
) -> str:
    return replay_arm_planning_service.build_arm_session_name(
        base_session_name=base_session_name,
        arm_metadata=arm_metadata,
        default_session_name=DEFAULT_SESSION_NAME,
    )


def _build_arm_run_environment(
    *,
    shared_run_environment: Mapping[str, Any],
    requested_model: str | None,
    session_name: str,
    arm_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return replay_arm_planning_service.build_arm_run_environment(
        shared_run_environment=shared_run_environment,
        requested_model=requested_model,
        session_name=session_name,
        arm_metadata=arm_metadata,
    )


def _run_prompt_replay_arm(
    *,
    prompt_entry: Mapping[str, Any],
    base_url: str,
    requested_model: str | None,
    timeout_seconds: float,
    poll_interval_seconds: float,
    user_concept_id: str,
    organisation_concept_id: str,
    base_session_name: str,
    shared_run_environment: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    arm_metadata: Mapping[str, Any] | None,
    presenter_mode: bool,
    gmail_profile: str | None,
    cancel_on_observer_expiry: bool = False,
) -> dict[str, Any]:
    session = requests.Session()
    session_name = _build_arm_session_name(
        base_session_name=base_session_name,
        arm_metadata=arm_metadata,
    )
    default_session_id, _window_session_id = _establish_authenticated_session(
        session=session,
        base_url=base_url,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        session_name=session_name,
    )
    run_environment = _build_arm_run_environment(
        shared_run_environment=shared_run_environment,
        requested_model=requested_model,
        session_name=session_name,
        arm_metadata=arm_metadata,
    )
    task_id, generate_payload = _run_generate_background(
        session=session,
        base_url=base_url,
        prompt=_safe_text(prompt_entry.get("prompt")),
        model=requested_model,
        gmail_profile=gmail_profile,
        presenter_mode=presenter_mode,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        include_status_payload=True,
        cancel_on_observer_expiry=cancel_on_observer_expiry,
    )
    request_id, session_id = _extract_request_and_session_ids(
        task_id=task_id,
        generate_payload=generate_payload,
        default_session_id=default_session_id,
    )
    response_text = (
        _safe_text(generate_payload.get("response"))
        or _safe_text(generate_payload.get("response_text"))
        or _safe_text(_as_mapping(generate_payload.get("llm_debug")).get("response"))
    )
    history_location, llm_debug_data = _resolve_turn_debug_data(
        session=session,
        base_url=base_url,
        session_id=session_id,
        request_id=request_id,
        response_text=response_text,
        generate_payload=generate_payload,
    )
    return _build_summary(
        prompt_entry=prompt_entry,
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
        history_location=history_location,
        generate_payload=generate_payload,
        llm_debug_data=llm_debug_data,
        prompt_bank_schema_version=prompt_bank_schema_version,
        requested_complexity_classes=requested_complexity_classes,
        seed=seed,
        requested_model=requested_model,
        run_environment=run_environment,
        arm_metadata=arm_metadata,
    )


def _build_multi_arm_summary(
    *,
    prompt_entry: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    requested_model: str | None,
    requested_model_arms: Sequence[Mapping[str, Any]],
    run_environment: Mapping[str, Any],
    arm_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    arm_observations: list[dict[str, Any]] = []
    collected_arm_count = 0
    for arm_summary in arm_summaries:
        if not isinstance(arm_summary, Mapping):
            continue
        arm = _as_mapping(arm_summary.get("arm"))
        label = _safe_text(arm.get("label")) or _safe_text(arm.get("arm_id"))
        telemetry = _as_mapping(arm_summary.get("telemetry"))
        response = _as_mapping(arm_summary.get("response"))
        collected = _safe_text(arm_summary.get("status")) == "ok"
        collected_arm_count += 1 if collected else 0
        arm_observations.append(
            {
                "arm_id": _safe_text(arm.get("arm_id")) or None,
                "arm_label": label or None,
                "status": _safe_text(arm_summary.get("status")) or None,
                "requested_model": _safe_text(arm.get("requested_model")) or None,
                "observed_model": _safe_text(telemetry.get("model")) or None,
                "ordinary_turn_terminal_status": _safe_text(
                    telemetry.get("ordinary_turn_terminal_status")
                )
                or None,
                "observed_tools": _as_list(telemetry.get("observed_tools")),
                "tool_count": telemetry.get("tool_count"),
                "timing": _as_mapping(telemetry.get("timing")),
                "response_length": len(_safe_text(response.get("text"))),
            }
        )
    arm_count = len(arm_summaries)
    pending_arm_count = sum(
        1
        for arm_summary in arm_summaries
        if _safe_text(_as_mapping(arm_summary).get("status")) == "pending"
    )
    all_arms_collected = bool(arm_count) and collected_arm_count == arm_count
    summary = {
        "status": (
            "ok"
            if all_arms_collected
            else (
                "pending"
                if pending_arm_count == arm_count and arm_count > 0
                else "partial"
            )
        ),
        "mode": "multi_arm_comparison",
        "guidance": {
            "replay_guide_path": REAL_PATH_REPLAY_GUIDE,
            "replay_guide_note": REAL_PATH_REPLAY_GUIDE_NOTE,
        },
        "environment": dict(run_environment),
        "selection": _build_selection_summary(
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            requested_model=requested_model,
            requested_model_arms=requested_model_arms,
        ),
        "prompt": _build_prompt_summary(prompt_entry),
        "comparison": {
            "arm_count": arm_count,
            "collected_arm_count": collected_arm_count,
            "pending_arm_count": pending_arm_count,
            "error_arm_count": (
                arm_count - collected_arm_count - pending_arm_count
            ),
            "all_arms_collected": all_arms_collected,
            "arm_observations": arm_observations,
        },
        "arms": [dict(entry) for entry in arm_summaries if isinstance(entry, Mapping)],
    }
    return summary


def _run_replay_plan(
    *,
    prompt_entry: Mapping[str, Any],
    base_url: str,
    requested_model: str | None,
    replay_arms: Sequence[Mapping[str, Any]],
    timeout_seconds: float,
    poll_interval_seconds: float,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
    run_environment: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    base_prompt_id: str | None,
    prompt_variant_ids: Sequence[Any],
    presenter_mode: bool,
    gmail_profile: str | None,
    cancel_on_observer_expiry: bool = False,
) -> tuple[dict[str, Any], bool]:
    if len(replay_arms) == 1:
        summary = _run_prompt_replay_arm(
            prompt_entry=prompt_entry,
            base_url=base_url,
            requested_model=requested_model,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            base_session_name=session_name,
            shared_run_environment=run_environment,
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            arm_metadata=(
                replay_arms[0] if (base_prompt_id or prompt_variant_ids) else None
            ),
            presenter_mode=presenter_mode,
            gmail_profile=gmail_profile,
            cancel_on_observer_expiry=cancel_on_observer_expiry,
        )
        return summary, _safe_text(summary.get("status")) == "ok"

    arm_summaries: list[dict[str, Any]] = []
    for arm_index, arm in enumerate(replay_arms, start=1):
        try:
            arm_summary = _run_prompt_replay_arm(
                prompt_entry=prompt_entry,
                base_url=base_url,
                requested_model=_safe_text(arm.get("requested_model")) or None,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                user_concept_id=user_concept_id,
                organisation_concept_id=organisation_concept_id,
                base_session_name=session_name,
                shared_run_environment=run_environment,
                prompt_bank_schema_version=prompt_bank_schema_version,
                requested_complexity_classes=requested_complexity_classes,
                seed=seed,
                arm_metadata=arm,
                presenter_mode=presenter_mode,
                gmail_profile=gmail_profile,
                cancel_on_observer_expiry=cancel_on_observer_expiry,
            )
        except Exception as exc:
            arm_summary = _build_failed_replay_attempt_summary(
                exc=exc,
                attempt_index=arm_index,
                prompt_entry=prompt_entry,
                run_environment=run_environment,
                requested_model=_safe_text(arm.get("requested_model")) or None,
                requested_model_arms=[arm],
            )
            arm_summary["arm"] = dict(arm)
        arm_summaries.append(arm_summary)
    summary = _build_multi_arm_summary(
        prompt_entry=prompt_entry,
        prompt_bank_schema_version=prompt_bank_schema_version,
        requested_complexity_classes=requested_complexity_classes,
        seed=seed,
        requested_model=requested_model,
        requested_model_arms=replay_arms,
        run_environment=run_environment,
        arm_summaries=arm_summaries,
    )
    collection_complete = bool(
        _as_mapping(summary.get("comparison")).get("all_arms_collected")
    )
    return summary, collection_complete


def _build_failed_replay_attempt_summary(
    *,
    exc: BaseException,
    attempt_index: int,
    prompt_entry: Mapping[str, Any],
    run_environment: Mapping[str, Any],
    requested_model: str | None,
    requested_model_arms: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if isinstance(exc, BackgroundGenerateTaskPending):
        pending = exc.to_report()
        return {
            "status": "pending",
            "attempt": {"attempt_index": attempt_index},
            "environment": dict(run_environment),
            "selection": {
                "requested_model": requested_model,
                "requested_provider": _infer_provider_from_model_identifier(
                    requested_model
                ),
            },
            "prompt": _build_prompt_summary(prompt_entry),
            "conversation": {
                "background_task_id": pending.get("task_id"),
                "request_id": pending.get("request_id"),
            },
            "response": {
                "text": "",
                "observation_pending": pending,
            },
            "telemetry": {
                "requested_model": _safe_text(requested_model) or None,
                "ordinary_turn_terminal_status": (
                    _safe_text(
                        _as_mapping(pending.get("status_payload")).get("status")
                    )
                    or "pending"
                ),
                "background_task_observations": pending,
                "observation_inconclusive": True,
            },
        }

    failure: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, BackgroundGenerateTaskError):
        failure["background_task"] = exc.to_report()
    planned_arms = [
        {
            "arm_id": _safe_text(arm.get("arm_id")) or None,
            "label": _safe_text(arm.get("label")) or None,
            "requested_model": _safe_text(arm.get("requested_model")) or None,
            "requested_provider": _safe_text(arm.get("requested_provider")) or None,
        }
        for arm in requested_model_arms
        if isinstance(arm, Mapping)
    ]
    summary = {
        "status": "error",
        "attempt": {"attempt_index": attempt_index},
        "environment": dict(run_environment),
        "selection": {
            "requested_model": requested_model,
            "requested_provider": _infer_provider_from_model_identifier(
                requested_model
            ),
            "requested_model_arms": planned_arms,
        },
        "prompt": _build_prompt_summary(prompt_entry),
        "conversation": {
            "background_task_id": _as_mapping(failure.get("background_task")).get(
                "task_id"
            )
        },
        "response": {"text": "", "failure": failure},
        "telemetry": {
            "requested_model": _safe_text(requested_model) or None,
            "ordinary_turn_terminal_status": _safe_text(
                _as_mapping(failure.get("background_task"))
                .get("status_payload", {})
                .get("status")
            )
            or None,
            "background_task_observations": (
                dict(_as_mapping(failure.get("background_task"))) or None
            ),
        },
    }
    if len(planned_arms) > 1:
        summary["comparison"] = {
            "planned_arm_count": len(planned_arms),
            "collected_arm_count": 0,
            "aborted_before_comparison_complete": True,
            "planned_arm_labels": [
                _safe_text(arm.get("label") or arm.get("arm_id"))
                for arm in planned_arms
                if _safe_text(arm.get("label") or arm.get("arm_id"))
            ],
        }
    return summary


def _build_repeated_replay_summary(
    *,
    prompt_entry: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    requested_model: str | None,
    requested_model_arms: Sequence[Mapping[str, Any]],
    run_environment: Mapping[str, Any],
    attempt_summaries: Sequence[Mapping[str, Any]],
    collection_count: int,
    minimum_collection_rate: float,
) -> dict[str, Any]:
    attempt_count = len(attempt_summaries)
    pending_attempt_count = sum(
        1
        for attempt in attempt_summaries
        if _safe_text(_as_mapping(attempt).get("status")) == "pending"
    )
    conclusive_attempt_count = attempt_count - pending_attempt_count
    collection_rate = (
        (float(collection_count) / float(conclusive_attempt_count))
        if conclusive_attempt_count
        else None
    )
    meets_minimum_collection_rate = (
        collection_rate >= minimum_collection_rate
        if collection_rate is not None
        else None
    )
    return {
        "status": (
            "pending"
            if conclusive_attempt_count == 0 and pending_attempt_count > 0
            else ("ok" if meets_minimum_collection_rate else "partial")
        ),
        "mode": "repeated_replay_suite",
        "guidance": {
            "replay_guide_path": REAL_PATH_REPLAY_GUIDE,
            "replay_guide_note": REAL_PATH_REPLAY_GUIDE_NOTE,
        },
        "environment": dict(run_environment),
        "selection": _build_selection_summary(
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            requested_model=requested_model,
            requested_model_arms=requested_model_arms,
        ),
        "prompt": _build_prompt_summary(prompt_entry),
        "repeat": {
            "attempt_count": attempt_count,
            "collected_attempt_count": collection_count,
            "pending_attempt_count": pending_attempt_count,
            "conclusive_attempt_count": conclusive_attempt_count,
            "error_attempt_count": conclusive_attempt_count - collection_count,
            "collection_rate": collection_rate,
            "minimum_collection_rate": minimum_collection_rate,
            "meets_minimum_collection_rate": meets_minimum_collection_rate,
            "metric": "harness_collection_only_not_semantic_success",
        },
        "attempts": [
            dict(attempt)
            for attempt in attempt_summaries
            if isinstance(attempt, Mapping)
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one sampled KB+tool-sensitive prompt against a live Von server "
            "and collect the response, tool, model, timing, and persisted turn "
            "observations without applying a Python semantic verdict. "
            f"Use in conjunction with {REAL_PATH_REPLAY_GUIDE}. "
            "Use --complexity-class to work up from easier direct prompts to "
            "KB-grounded and then tool-augmented prompts."
        )
    )
    parser.add_argument(
        "--base-url",
        default=get_default_agent_test_base_url(),
        help=(
            "Live Von server base URL. Defaults to the JVNAUTOSCI-2070 "
            f"isolated agent-test server ({DEFAULT_AGENT_TEST_BASE_URL}); set "
            f"{AGENT_TEST_BASE_URL_ENV_VAR} or pass this flag when using a "
            "different -AgentTest -Port value."
        ),
    )
    parser.add_argument(
        "--allow-non-agent-test-server",
        action="store_true",
        help=(
            "Allow the replay to target a server whose /health response does "
            "not report agent_test_instance=true. Use only when deliberately "
            "testing the interactive/user-facing server."
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            "Explicit model override for the replay. Defaults to Ollama "
            "`gemma4:31b` for the JVNAUTOSCI-1894 replay programme. Pass an "
            "empty string to omit the override and let /von/generate use the "
            "active user-facing model for the authenticated session."
        ),
    )
    parser.add_argument(
        "--compare-model",
        dest="compare_models",
        action="append",
        default=[],
        help=(
            "Additional model override to run as a comparison arm. Repeat the "
            "flag to compare the same prompt and authenticated context across "
            "multiple requested models."
        ),
    )
    parser.add_argument(
        "--include-active-model-arm",
        action="store_true",
        help=(
            "When running comparison arms, also replay one arm without any model "
            "override so the authenticated session's active user-facing model is "
            "measured alongside explicit requested-model arms."
        ),
    )
    parser.add_argument(
        "--repeat-count",
        type=int,
        default=1,
        help=(
            "Run the same selected prompt/arm plan repeatedly and report the "
            "mechanical collection rate."
        ),
    )
    parser.add_argument(
        "--minimum-collection-rate",
        dest="minimum_collection_rate",
        type=float,
        default=DEFAULT_MINIMUM_COLLECTION_RATE,
        help=(
            "Required repeated-suite collection rate; this is not a semantic "
            "answer-quality threshold. Default 0.95."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=900.0,
        help=(
            "Background-task observation window. Elapsing it leaves the task "
            "running and records a pending reconciliation receipt by default."
        ),
    )
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument(
        "--cancel-on-observer-expiry",
        action="store_true",
        help=(
            "Explicitly cancel a still-live server task after the sampler's "
            "observation window. The default records it as pending and leaves "
            "it available for reconciliation."
        ),
    )
    parser.add_argument("--user-concept-id", default=DEFAULT_USER_CONCEPT_ID)
    parser.add_argument(
        "--organisation-concept-id", default=DEFAULT_ORGANISATION_CONCEPT_ID
    )
    parser.add_argument("--session-name", default=DEFAULT_SESSION_NAME)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--prompt-id", default="")
    parser.add_argument(
        "--prompt-text",
        default="",
        help=(
            "Use an explicit prompt text instead of sampling the prompt bank. "
            "Useful for fixed failure-case replays after a workflow has already "
            "resolved the replay case."
        ),
    )
    parser.add_argument(
        "--replay-case-id",
        default="",
        help="Stable replay case id to attach to reports for explicit prompt/failure-case runs.",
    )
    parser.add_argument(
        "--replay-set-id",
        default="",
        help="Replay set id to attach to prompt-variant arm metadata.",
    )
    parser.add_argument(
        "--workflow-id",
        default="",
        help="Expected or target workflow id for replay-case provenance.",
    )
    parser.add_argument(
        "--workflow-stage-id",
        default="",
        help="Workflow stage id whose prompt variants are being evaluated.",
    )
    parser.add_argument(
        "--base-prompt-id",
        default="",
        help="Base Vontology prompt concept id for prompt-variant replay arms.",
    )
    parser.add_argument(
        "--prompt-variant-id",
        dest="prompt_variant_ids",
        action="append",
        default=[],
        help=(
            "Candidate represented prompt variant concept id. Repeat to create "
            "candidate arms. The runner records whether normal runtime prompt "
            "variant resolution selected this id; it does not inject raw prompt text."
        ),
    )
    parser.add_argument(
        "--experiment-run-id",
        default="",
        help=(
            "Existing experiment_run concept id. When supplied, replay arm "
            "observations are appended through the internal MCP experiment surface."
        ),
    )
    parser.add_argument(
        "--failure-conversation-ref-json",
        default="",
        help=(
            "JSON object, path, or @path for a conversation_ref accepted by "
            "failure-case intake."
        ),
    )
    parser.add_argument(
        "--failure-chat-history-lookup-json",
        default="",
        help="JSON object, path, or @path for optional chat_history_lookup context.",
    )
    parser.add_argument(
        "--failure-request-id",
        default="",
        help="Existing failed turn request_id to resolve into a replay prompt.",
    )
    parser.add_argument(
        "--failure-current-request-id",
        default="",
        help="Current request id to exclude when resolving same-conversation failure references.",
    )
    parser.add_argument(
        "--failure-reference-mode",
        default="",
        help=(
            "Failure reference mode, for example latest_prior_failure, when no "
            "exact failure request id is supplied."
        ),
    )
    parser.add_argument(
        "--failure-reference-phrase",
        default="",
        help="User phrase that triggered same-conversation failure-case resolution.",
    )
    parser.add_argument(
        "--namespace",
        default="",
        help="Namespace to use for failure-case intake and experiment provenance.",
    )
    parser.add_argument(
        "--include-legacy-history",
        action="store_true",
        help="Allow failure-case intake to include legacy chat-history records.",
    )
    parser.add_argument(
        "--history-tail-limit",
        type=int,
        default=None,
        help="Optional history tail limit for failure-case intake.",
    )
    parser.add_argument(
        "--complexity-class",
        dest="complexity_classes",
        action="append",
        choices=tuple(PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS),
        help=(
            "Restrict random selection to one prompt complexity class. "
            "Repeat the flag to allow more than one class."
        ),
    )
    parser.add_argument("--output-json", default="")
    parser.add_argument(
        "--presenter-mode",
        action="store_true",
        help="Send presenter_mode=true to /von/generate to match the browser chat path.",
    )
    parser.add_argument(
        "--gmail-profile",
        default="",
        help=(
            "Optional configured Gmail profile alias to send to /von/generate "
            "for Gmail-backed replay prompts."
        ),
    )
    parser.add_argument("--list-prompts", action="store_true")
    parser.add_argument("--list-complexity-classes", action="store_true")
    args = parser.parse_args(argv)

    _emit_replay_guide_note()

    prompt_bank_payload = _load_prompt_bank()
    prompt_bank_schema_version = _safe_text(prompt_bank_payload.get("schema_version"))
    prompt_bank = _as_list(prompt_bank_payload.get("prompts"))
    requested_complexity_classes = [
        name
        for name in _as_list(args.complexity_classes)
        if isinstance(name, str) and name in PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS
    ]
    allowed_complexity_classes = frozenset(requested_complexity_classes)
    if args.list_complexity_classes:
        print(
            json.dumps(
                PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.list_prompts:
        print(
            json.dumps(
                [
                    {
                        "id": _safe_text(entry.get("id")),
                        "category": _safe_text(entry.get("category")),
                        "complexity_class": _safe_text(entry.get("complexity_class")),
                        "prompt": _safe_text(entry.get("prompt")),
                        "requires_tool_use": bool(entry.get("requires_tool_use")),
                        "allows_grounded_empty_result": bool(
                            entry.get("allows_grounded_empty_result")
                        ),
                    }
                    for entry in prompt_bank
                    if isinstance(entry, Mapping)
                    and (
                        not allowed_complexity_classes
                        or _safe_text(entry.get("complexity_class"))
                        in allowed_complexity_classes
                    )
                ],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    base_url = resolve_live_test_base_url(args.base_url)
    requested_model = _safe_text(args.model) or None
    requested_gmail_profile = _safe_text(args.gmail_profile) or None
    compare_models = [
        cleaned
        for entry in _as_list(args.compare_models)
        if isinstance(entry, str) and (cleaned := _safe_text(entry))
    ]
    model_arms = _build_model_arm_plan(
        requested_model=requested_model,
        compare_models=compare_models,
        include_active_model_arm=bool(args.include_active_model_arm),
    )
    replay_case_id = _safe_text(args.replay_case_id) or None
    replay_set_id = _safe_text(args.replay_set_id) or None
    target_workflow_id = _safe_text(args.workflow_id) or None
    workflow_stage_id = _safe_text(args.workflow_stage_id) or None
    base_prompt_id = _safe_text(args.base_prompt_id) or None
    prompt_variant_ids = _dedupe_texts(_as_list(args.prompt_variant_ids))
    failure_case_intake: dict[str, Any] | None = None
    explicit_prompt_text = _safe_text(args.prompt_text)
    failure_conversation_ref = _load_json_mapping_argument(
        args.failure_conversation_ref_json,
        argument_name="--failure-conversation-ref-json",
    )
    failure_chat_history_lookup = _load_json_mapping_argument(
        args.failure_chat_history_lookup_json,
        argument_name="--failure-chat-history-lookup-json",
    )
    failure_request_id = _safe_text(args.failure_request_id) or None
    failure_reference_mode = _safe_text(args.failure_reference_mode) or None
    failure_reference_phrase = _safe_text(args.failure_reference_phrase) or None
    if (
        failure_conversation_ref
        or failure_chat_history_lookup
        or failure_request_id
        or failure_reference_mode
    ):
        prompt_entry, failure_case_intake = _collect_failure_case_prompt_entry(
            conversation_ref=failure_conversation_ref or None,
            chat_history_lookup=failure_chat_history_lookup or None,
            request_id=failure_request_id,
            session_id=None,
            namespace=_safe_text(args.namespace) or None,
            user_concept_id=_safe_text(args.user_concept_id) or None,
            organisation_concept_id=_safe_text(args.organisation_concept_id) or None,
            target_model=requested_model,
            comparator_model=compare_models[0] if compare_models else None,
            workflow_id=target_workflow_id,
            stage_id=workflow_stage_id,
            current_request_id=_safe_text(args.failure_current_request_id) or None,
            reference_mode=failure_reference_mode,
            reference_phrase=failure_reference_phrase,
            include_legacy=bool(args.include_legacy_history),
            history_tail_limit=args.history_tail_limit,
            replay_case_id=replay_case_id,
        )
        replay_case_id = _safe_text(prompt_entry.get("id")) or replay_case_id
        target_workflow_id = (
            target_workflow_id
            or _safe_text(prompt_entry.get("source_workflow_id"))
            or None
        )
    elif explicit_prompt_text:
        prompt_entry = _normalise_prompt_entry(
            prompt_text=explicit_prompt_text,
            replay_case_id=replay_case_id or f"ad_hoc_replay_{uuid.uuid4().hex[:12]}",
            category="ad_hoc_replay",
            likely_tools=[],
            knowledge_surfaces=["turn_context"],
            requires_tool_use=False,
            source_kind="explicit_prompt_text",
        )
        replay_case_id = _safe_text(prompt_entry.get("id")) or replay_case_id
    else:
        prompt_entry = _choose_prompt(
            prompt_bank,
            seed=args.seed,
            prompt_id=_safe_text(args.prompt_id) or None,
            allowed_complexity_classes=allowed_complexity_classes,
        )
    replay_arms = _build_replay_arm_plan(
        model_arms=model_arms,
        base_prompt_id=base_prompt_id,
        prompt_variant_ids=prompt_variant_ids,
        workflow_stage_id=workflow_stage_id,
        target_workflow_id=target_workflow_id,
        replay_set_id=replay_set_id,
        replay_case_id=replay_case_id,
    )
    authenticated_user_concept_id = (
        _safe_text(args.user_concept_id) or DEFAULT_USER_CONCEPT_ID
    )
    authenticated_organisation_concept_id = (
        _safe_text(args.organisation_concept_id) or DEFAULT_ORGANISATION_CONCEPT_ID
    )
    session_name = _safe_text(args.session_name) or DEFAULT_SESSION_NAME
    run_environment = _collect_run_environment(
        base_url=base_url,
        requested_model=requested_model if len(model_arms) == 1 else None,
        user_concept_id=authenticated_user_concept_id,
        organisation_concept_id=authenticated_organisation_concept_id,
        session_name=session_name,
    )
    metadata_session = requests.Session()
    run_environment = _augment_run_environment_with_server_diag(
        session=metadata_session,
        base_url=base_url,
        run_environment=run_environment,
    )
    if not bool(args.allow_non_agent_test_server):
        agent_test_error = build_agent_test_server_requirement_error(
            run_environment,
            base_url=base_url,
        )
        if agent_test_error:
            raise RuntimeError(agent_test_error)
    if _replay_arms_require_active_llm_info(replay_arms):
        run_environment = _augment_run_environment_with_active_llm_info(
            session=metadata_session,
            base_url=base_url,
            user_concept_id=authenticated_user_concept_id,
            organisation_concept_id=authenticated_organisation_concept_id,
            run_environment=run_environment,
        )
    else:
        run_environment = {
            **run_environment,
            "server_resolved_active_llm_lookup_skipped": True,
            "server_resolved_active_llm_lookup_skip_reason": (
                "explicit_model_override_arms_only"
            ),
        }
    repeat_count = max(int(args.repeat_count or 1), 1)
    minimum_collection_rate = min(max(float(args.minimum_collection_rate), 0.0), 1.0)
    if repeat_count == 1:
        try:
            summary, collection_complete = _run_replay_plan(
                prompt_entry=prompt_entry,
                base_url=base_url,
                requested_model=requested_model,
                replay_arms=replay_arms,
                timeout_seconds=float(args.timeout_seconds),
                poll_interval_seconds=float(args.poll_interval_seconds),
                user_concept_id=authenticated_user_concept_id,
                organisation_concept_id=authenticated_organisation_concept_id,
                session_name=session_name,
                run_environment=run_environment,
                prompt_bank_schema_version=prompt_bank_schema_version,
                requested_complexity_classes=requested_complexity_classes,
                seed=args.seed,
                base_prompt_id=base_prompt_id,
                prompt_variant_ids=prompt_variant_ids,
                presenter_mode=bool(args.presenter_mode),
                gmail_profile=requested_gmail_profile,
                cancel_on_observer_expiry=bool(
                    args.cancel_on_observer_expiry
                ),
            )
        except Exception as exc:
            summary = _build_failed_replay_attempt_summary(
                exc=exc,
                attempt_index=1,
                prompt_entry=prompt_entry,
                run_environment=run_environment,
                requested_model=requested_model,
                requested_model_arms=replay_arms,
            )
            collection_complete = False
    else:
        attempt_summaries: list[dict[str, Any]] = []
        collected_attempt_count = 0
        for attempt_index in range(1, repeat_count + 1):
            try:
                attempt_summary, attempt_success = _run_replay_plan(
                    prompt_entry=prompt_entry,
                    base_url=base_url,
                    requested_model=requested_model,
                    replay_arms=replay_arms,
                    timeout_seconds=float(args.timeout_seconds),
                    poll_interval_seconds=float(args.poll_interval_seconds),
                    user_concept_id=authenticated_user_concept_id,
                    organisation_concept_id=authenticated_organisation_concept_id,
                    session_name=session_name,
                    run_environment=run_environment,
                    prompt_bank_schema_version=prompt_bank_schema_version,
                    requested_complexity_classes=requested_complexity_classes,
                    seed=args.seed,
                    base_prompt_id=base_prompt_id,
                    prompt_variant_ids=prompt_variant_ids,
                    presenter_mode=bool(args.presenter_mode),
                    gmail_profile=requested_gmail_profile,
                    cancel_on_observer_expiry=bool(
                        args.cancel_on_observer_expiry
                    ),
                )
                attempt_summary = {
                    **attempt_summary,
                    "attempt": {"attempt_index": attempt_index},
                }
                collected_attempt_count += 1 if attempt_success else 0
                attempt_summaries.append(attempt_summary)
                if _safe_text(attempt_summary.get("status")) == "pending":
                    break
            except Exception as exc:
                incomplete_summary = _build_failed_replay_attempt_summary(
                    exc=exc,
                    attempt_index=attempt_index,
                    prompt_entry=prompt_entry,
                    run_environment=run_environment,
                    requested_model=requested_model,
                    requested_model_arms=replay_arms,
                )
                attempt_summaries.append(incomplete_summary)
                if _safe_text(incomplete_summary.get("status")) == "pending":
                    break
        summary = _build_repeated_replay_summary(
            prompt_entry=prompt_entry,
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=args.seed,
            requested_model=requested_model,
            requested_model_arms=replay_arms,
            run_environment=run_environment,
            attempt_summaries=attempt_summaries,
            collection_count=collected_attempt_count,
            minimum_collection_rate=minimum_collection_rate,
        )
        collection_complete = bool(
            _as_mapping(summary.get("repeat")).get("meets_minimum_collection_rate")
        )
    if failure_case_intake is not None:
        summary["failure_case_intake"] = failure_case_intake
    experiment_run_id = _safe_text(args.experiment_run_id)
    if experiment_run_id:
        if repeat_count > 1:
            arm_summaries_for_recording = []
            for attempt in _as_list(summary.get("attempts")):
                if not isinstance(attempt, Mapping) or attempt.get("status") == "error":
                    continue
                if isinstance(attempt.get("arms"), Sequence):
                    arm_summaries_for_recording.extend(_as_list(attempt.get("arms")))
                else:
                    arm_summaries_for_recording.append(attempt)
        else:
            arm_summaries_for_recording = (
                _as_list(summary.get("arms"))
                if isinstance(summary.get("arms"), Sequence)
                else [summary]
            )
        experiment_recording = _record_experiment_observations(
            run_id=experiment_run_id,
            arm_summaries=[
                entry
                for entry in arm_summaries_for_recording
                if isinstance(entry, Mapping)
            ],
        )
        summary["experiment_recording"] = experiment_recording
        if not bool(experiment_recording.get("success")):
            raise RuntimeError(
                "Experiment observation recording failed: "
                f"{json.dumps(experiment_recording, ensure_ascii=True, sort_keys=True)}"
            )
    output_json = _safe_text(args.output_json)
    if output_json:
        _write_json_output(output_json, summary)
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return (
        0
        if collection_complete
        or _safe_text(summary.get("status")) in {"pending", "inconclusive"}
        else 1
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI surface
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
