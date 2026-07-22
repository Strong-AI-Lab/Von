"""Authenticated real-path replay harness for browser-backed workflow acceptance.

JVNAUTOSCI-2422

The harness establishes a real Flask session through Von's local browser-test
login endpoint, submits a chat turn through `/von/generate`, polls both the
background task and live Thinking-card progress endpoint, and emits a compact
JSON evidence report. Workflow-specific expectations are replay-case data; the
request path remains generic.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from typing import Any
import uuid

import requests

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.live_test_server_defaults import (  # noqa: E402
    build_agent_test_server_requirement_error,
    get_default_agent_test_base_url,
)
from src.backend.services.agent_test_replay_mode_service import (  # noqa: E402
    AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY,
)


GMAIL_ARXIV_PROMPT = (
    "Look in the last 20 email messages for arXiv papers and represent any "
    "papers you find in Vontology."
)
GMAIL_ARXIV_WORKFLOW_ID = "#V#zhan_gmail_arxiv_ingestion_workflow"
GMAIL_ARXIV_FACT_IDS = (
    "gmail_message_subject",
    "arxiv_paper_id",
    "arxiv_paper_title",
    "arxiv_paper_concept",
    "paper_concept",
    "message_arxiv_result",
    "arxiv_resource_result",
    "paper_representation_result",
    "gmail_arxiv_batch_result",
    "gmail_arxiv_message_count",
    "gmail_arxiv_success_count",
    "gmail_arxiv_error_count",
)
GMAIL_ARXIV_CONTRACT_IDS = (
    "#V#gmail_arxiv_email_message_subject_progress_fact",
    "#V#gmail_arxiv_paper_id_progress_fact",
    "#V#gmail_arxiv_paper_title_progress_fact",
    "#V#gmail_arxiv_paper_concept_progress_fact",
    "#V#gmail_arxiv_general_paper_concept_progress_fact",
    "#V#gmail_arxiv_message_result_progress_fact",
    "#V#gmail_arxiv_resource_result_progress_fact",
    "#V#gmail_arxiv_paper_representation_result_progress_fact",
    "#V#gmail_arxiv_batch_result_progress_fact",
    "#V#gmail_arxiv_messages_scanned_progress_fact",
    "#V#gmail_arxiv_messages_completed_progress_fact",
    "#V#gmail_arxiv_messages_failed_progress_fact",
)

TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class ReplayCase:
    case_id: str
    prompt: str
    expected_workflow_id: str | None
    expected_progress_fact_ids: tuple[str, ...]
    expected_contract_ids: tuple[str, ...]
    requires_gmail: bool = False
    workflow_inputs: Mapping[str, Any] | None = None


GMAIL_ARXIV_REPLAY_CASE = ReplayCase(
    case_id="gmail_arxiv_ingestion_2421_motivating_prompt",
    prompt=GMAIL_ARXIV_PROMPT,
    expected_workflow_id=GMAIL_ARXIV_WORKFLOW_ID,
    expected_progress_fact_ids=GMAIL_ARXIV_FACT_IDS,
    expected_contract_ids=GMAIL_ARXIV_CONTRACT_IDS,
    requires_gmail=True,
)


@dataclass(frozen=True)
class ReplayTurnCase:
    turn_id: str
    case: ReplayCase


GMAIL_ARXIV_IDEMPOTENCE_CASE_ID = "gmail-arxiv-idempotence-2571"
GMAIL_ARXIV_IDEMPOTENCE_REPLAY_CASE_ID = "gmail_arxiv_idempotence_2571_sequence"
GMAIL_ARXIV_IDEMPOTENCE_DEFAULT_QUERY = "arxiv.org newer_than:365d"
GMAIL_ARXIV_IDEMPOTENCE_PROCESSING_MARKER = "represented message-processing evidence"
GMAIL_MUTATION_TOOL_NAMES = frozenset(
    {
        "gmail_create_label",
        "gmail_modify_labels",
        "gmail_send_message",
        "gmail_set_profile_scope",
    }
)
GMAIL_ARXIV_IDEMPOTENCE_TURN_PROMPTS: tuple[tuple[str, str], ...] = (
    (
        "initial",
        "Use the represented Gmail-to-arXiv ingestion workflow for at most one "
        "recent Gmail message matching `{gmail_query}` whose represented Von "
        "message-processing evidence does not already show completed handling. "
        "Represent the arXiv paper from that message in Vontology, then answer "
        "with the arXiv ID, paper concept, file-copy or import evidence if "
        "available, the Gmail message or thread identifier you used, and the "
        "represented message-processing evidence. Do not archive, delete, "
        "label, reply, send, create labels, or change Gmail settings.",
    ),
    (
        "repeat",
        "Represent that same arXiv paper from the email again. If the paper is "
        "already represented or the source Gmail message already has "
        "represented processing evidence, read back the existing concept, "
        "file-copy/import, and message-processing evidence instead of creating "
        "a duplicate or selecting a different paper. Do not archive, delete, "
        "label, reply, send, create labels, or change Gmail settings.",
    ),
    (
        "verify",
        "Show me the represented paper concept and whether that Gmail message "
        "or thread was already processed for this paper. Read back existing "
        "evidence; do not recreate the paper, archive, delete, label, reply, "
        "send, create labels, or change Gmail settings.",
    ),
)


class JsonRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = dict(payload or {})


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _normalise_concept_id(value: Any) -> str | None:
    text = _safe_text(value)
    if not text:
        return None
    return text if text.startswith("#V#") else f"#V#{text}"


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _git_capture(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=_PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _git_status_dirty() -> bool | None:
    """Return an explicit clean/dirty result while preserving command failure."""

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=_PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    expected_status: int | Sequence[int] = 200,
    timeout_seconds: float = 120.0,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        response = session.request(
            str(method or "GET").upper(),
            url,
            timeout=max(float(timeout_seconds), 1.0),
            **kwargs,
        )
    except requests.RequestException as exc:
        raise JsonRequestError(f"{method} {url} failed: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise JsonRequestError(
            f"{method} {url} returned non-JSON status {response.status_code}: "
            f"{response.text[:500]}",
            status_code=response.status_code,
        ) from exc
    expected = (
        {int(expected_status)}
        if isinstance(expected_status, int)
        else {int(status) for status in expected_status}
    )
    if response.status_code not in expected:
        raise JsonRequestError(
            f"{method} {url} returned status {response.status_code}",
            status_code=response.status_code,
            payload=payload,
        )
    return payload


def _summarise_server_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    version_details = _as_mapping(payload.get("version_details"))
    runtime_authority = _as_mapping(payload.get("runtime_authority"))
    return {
        "version": _safe_text(payload.get("version")) or None,
        "git_branch": _safe_text(version_details.get("git_branch")) or None,
        "git_commit": _safe_text(version_details.get("git_commit")) or None,
        "git_dirty": version_details.get("git_dirty"),
        "agent_test_instance": payload.get("agent_test_instance"),
        "represented_postcondition_critic_enabled": payload.get(
            "represented_postcondition_critic_enabled"
        ),
        "runtime_authority": dict(runtime_authority) or None,
        "effective_user_concept_id": (
            _safe_text(payload.get("effective_user_concept_id")) or None
        ),
        "session_user_concept_id": (
            _safe_text(payload.get("session_user_concept_id")) or None
        ),
        "header_user_concept_id": (
            _safe_text(payload.get("header_user_concept_id")) or None
        ),
    }


def collect_run_environment(
    *,
    session: requests.Session,
    base_url: str,
) -> dict[str, Any]:
    environment: dict[str, Any] = {
        "run_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "local_python_executable": sys.executable,
        "local_python_version": platform.python_version(),
        "local_platform": platform.platform(),
        "local_repo_git_branch": _git_capture("rev-parse", "--abbrev-ref", "HEAD"),
        "local_repo_git_head": _git_capture("rev-parse", "HEAD"),
        "local_repo_git_dirty": _git_status_dirty(),
    }
    for source, path in (("health", "/health"), ("diag", "/diag")):
        try:
            payload = _request_json(
                session,
                "GET",
                f"{base_url}{path}",
                timeout_seconds=12.0,
            )
        except Exception as exc:
            environment["server_metadata_error"] = str(exc)
            continue
        environment.update(
            {
                "server_metadata_source": source,
                "server_metadata_error": None,
                **{
                    f"server_{key}": value
                    for key, value in _summarise_server_payload(payload).items()
                },
            }
        )
        break
    return environment


def require_agent_test_server(
    *,
    environment: Mapping[str, Any],
    base_url: str,
    allow_non_agent_test_server: bool,
) -> None:
    if allow_non_agent_test_server:
        return
    error = build_agent_test_server_requirement_error(
        {
            "server_agent_test_instance": environment.get("server_agent_test_instance"),
            "server_metadata_source": environment.get("server_metadata_source"),
            "server_metadata_error": environment.get("server_metadata_error"),
        },
        base_url=base_url,
    )
    if error:
        raise RuntimeError(error)


def get_auth_status(
    *,
    session: requests.Session,
    base_url: str,
) -> dict[str, Any]:
    try:
        return _request_json(session, "GET", f"{base_url}/von/api/auth/status")
    except JsonRequestError as exc:
        return {
            "authenticated": False,
            "error": str(exc),
            "status_code": exc.status_code,
            "payload": exc.payload,
        }


def establish_browser_test_session(
    *,
    session: requests.Session,
    base_url: str,
    window_session_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    session.headers.update({"X-Von-Window-Session": window_session_id})
    try:
        return _request_json(
            session,
            "POST",
            f"{base_url}/von/api/auth/browser-test-login",
            expected_status=200,
            timeout_seconds=timeout_seconds,
            json={
                "window_session_id": window_session_id,
                "refresh_fixture": False,
            },
        )
    except Exception as exc:
        payload = exc.payload if isinstance(exc, JsonRequestError) else {}
        return {
            "success": False,
            "error": str(exc),
            "status_code": exc.status_code
            if isinstance(exc, JsonRequestError)
            else None,
            "payload": payload,
            "browser_test_mode": _as_mapping(payload.get("browser_test_mode")),
        }


def apply_target_session_context(
    *,
    session: requests.Session,
    base_url: str,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
) -> dict[str, Any]:
    """Align the replay session with the requested target user/org context."""

    result: dict[str, Any] = {
        "requested_user_concept_id": _safe_text(user_concept_id) or None,
        "requested_organisation_concept_id": (
            _safe_text(organisation_concept_id) or None
        ),
        "updates": [],
    }
    target_user = _safe_text(user_concept_id)
    target_org = _safe_text(organisation_concept_id)
    if target_user:
        session.headers.update({"X-User-Concept-ID": target_user})
        try:
            result["updates"].append(
                {
                    "step": "set_user_concept",
                    "payload": _request_json(
                        session,
                        "POST",
                        f"{base_url}/von/api/session/set_user_concept",
                        timeout_seconds=20.0,
                        json={"user_concept_id": target_user},
                    ),
                }
            )
        except JsonRequestError as exc:
            result["updates"].append(
                {
                    "step": "set_user_concept",
                    "error": str(exc),
                    "status_code": exc.status_code,
                    "payload": exc.payload,
                }
            )
    if target_org:
        try:
            result["updates"].append(
                {
                    "step": "set_organisation",
                    "payload": _request_json(
                        session,
                        "POST",
                        f"{base_url}/von/api/session/set_organisation",
                        timeout_seconds=20.0,
                        json={"organisation_concept_id": target_org},
                    ),
                }
            )
        except JsonRequestError as exc:
            result["updates"].append(
                {
                    "step": "set_organisation",
                    "error": str(exc),
                    "status_code": exc.status_code,
                    "payload": exc.payload,
                }
            )
    try:
        result["session_context"] = _request_json(
            session,
            "GET",
            f"{base_url}/von/api/session/context",
            timeout_seconds=20.0,
        )
    except JsonRequestError as exc:
        result["session_context"] = {
            "authenticated": False,
            "error": str(exc),
            "status_code": exc.status_code,
            "payload": exc.payload,
        }
    context = _as_mapping(result.get("session_context"))
    update_errors = [
        item
        for item in _as_list(result.get("updates"))
        if isinstance(item, Mapping) and item.get("error")
    ]
    requested_user = _normalise_concept_id(user_concept_id)
    requested_org = _normalise_concept_id(organisation_concept_id)
    actual_user = _normalise_concept_id(context.get("user_id"))
    actual_org = _normalise_concept_id(context.get("organisation_id"))
    mismatch_reasons: list[str] = []
    if requested_user and actual_user != requested_user:
        mismatch_reasons.append(
            f"requested user {requested_user} but effective user is {actual_user}"
        )
    if requested_org and actual_org != requested_org:
        mismatch_reasons.append(
            f"requested organisation {requested_org} but effective organisation is {actual_org}"
        )
    result["target_session_context_ready"] = bool(
        context.get("authenticated") is True
        and not update_errors
        and not mismatch_reasons
    )
    if mismatch_reasons:
        result["mismatch_reasons"] = mismatch_reasons
    if update_errors:
        result["update_errors"] = [dict(item) for item in update_errors]
    return result


def create_replay_chat_session(
    *,
    session: requests.Session,
    base_url: str,
    case_id: str,
) -> dict[str, Any]:
    payload = {
        "session_name": f"JVNAUTOSCI-2422 replay {case_id}",
        "origin_kind": "coding_agent_test",
        "created_by_actor_type": "#V#coding_agent",
        "created_by_actor_concept_id": "#V#von_system",
        "is_agent_created": True,
        "test_artifact_kind": "authenticated_browser_workflow_replay",
    }
    return _request_json(
        session,
        "POST",
        f"{base_url}/von/api/session/create_chat_session",
        json=payload,
    )


def _query_settings(
    *,
    session: requests.Session,
    base_url: str,
) -> dict[str, Any]:
    try:
        return _request_json(
            session,
            "GET",
            f"{base_url}/api/settings/",
            timeout_seconds=20.0,
        )
    except Exception as exc:
        return {"error": str(exc)}


def _query_gmail_oauth_status(
    *,
    session: requests.Session,
    base_url: str,
    gmail_profile: str | None,
) -> dict[str, Any] | None:
    profile = _safe_text(gmail_profile)
    if not profile:
        return None
    try:
        return _request_json(
            session,
            "GET",
            f"{base_url}/von/api/agent/gmail/oauth/status",
            timeout_seconds=20.0,
            params={"profile_id": profile},
        )
    except JsonRequestError as exc:
        return {
            "error": str(exc),
            "status_code": exc.status_code,
            "payload": exc.payload,
        }


def _query_gmail_access_test(
    *,
    session: requests.Session,
    base_url: str,
    gmail_profile: str | None,
) -> dict[str, Any] | None:
    profile = _safe_text(gmail_profile)
    if not profile:
        return None
    try:
        return _request_json(
            session,
            "POST",
            f"{base_url}/von/api/agent/gmail/oauth/test_access",
            timeout_seconds=30.0,
            params={"profile_id": profile},
        )
    except JsonRequestError as exc:
        return {
            "success": False,
            "error": str(exc),
            "status_code": exc.status_code,
            "payload": exc.payload,
        }


def build_gmail_preflight(
    *,
    session: requests.Session,
    base_url: str,
    gmail_profile: str | None,
    required: bool = True,
) -> dict[str, Any]:
    if not required:
        return {
            "required": False,
            "gmail_capability_ready": True,
            "reason": "Replay case does not require Gmail.",
        }
    settings = _query_settings(session=session, base_url=base_url)
    profiles = _as_list(settings.get("gmail_profiles"))
    default_profile = _safe_text(settings.get("gmail_default_profile")) or None
    requested_profile = _safe_text(gmail_profile) or default_profile
    oauth_status = _query_gmail_oauth_status(
        session=session,
        base_url=base_url,
        gmail_profile=requested_profile,
    )
    requested_profile_configured = (
        bool(requested_profile) and requested_profile in profiles
    )
    token_status_ready = bool(
        requested_profile_configured
        and isinstance(oauth_status, Mapping)
        and oauth_status.get("has_tokens") is True
    )
    access_test = (
        _query_gmail_access_test(
            session=session,
            base_url=base_url,
            gmail_profile=requested_profile,
        )
        if token_status_ready
        else None
    )
    return {
        "requested_gmail_profile": requested_profile,
        "required": True,
        "configured_gmail_profiles": profiles,
        "default_gmail_profile": default_profile,
        "gmail_profiles_configured": bool(profiles),
        "requested_profile_configured": requested_profile_configured,
        "oauth_status": oauth_status,
        "access_test": access_test,
        "gmail_capability_ready": bool(
            token_status_ready
            and isinstance(access_test, Mapping)
            and access_test.get("success") is True
        ),
    }


def _db_probe_ready(payload: Mapping[str, Any]) -> bool:
    probe = _as_mapping(payload.get("probe"))
    return bool(
        payload.get("connected") is True
        and probe.get("ok") is True
        and probe.get("ping_ok") is True
        and probe.get("read_ok") is True
        and probe.get("write_ok") is not False
    )


def build_database_preflight(
    *,
    session: requests.Session,
    base_url: str,
    attempt_count: int = 3,
    poll_interval_seconds: float = 0.75,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    total_attempts = max(int(attempt_count or 0), 1)
    for index in range(total_attempts):
        started_at = time.monotonic()
        try:
            payload = _request_json(
                session,
                "GET",
                f"{base_url}/admin/db/health",
                expected_status=(200, 503),
                timeout_seconds=60.0,
                params={"probe": "rw"},
            )
        except JsonRequestError as exc:
            payload = {
                "connected": False,
                "error": str(exc),
                "status_code": exc.status_code,
                "payload": exc.payload,
            }
        except Exception as exc:
            payload = {
                "connected": False,
                "error": str(exc),
            }
        attempt = dict(payload)
        attempt["attempt"] = index + 1
        attempt["elapsed_ms"] = round((time.monotonic() - started_at) * 1000.0, 3)
        attempt["database_runtime_available"] = _db_probe_ready(attempt)
        attempts.append(attempt)
        if index + 1 < total_attempts:
            time.sleep(max(float(poll_interval_seconds or 0.0), 0.0))

    latest = dict(attempts[-1])
    latest["database_runtime_available"] = bool(
        attempts and all(_db_probe_ready(item) for item in attempts)
    )
    latest["preflight_attempts"] = attempts
    latest["preflight_wait"] = {
        "attempt_count": total_attempts,
        "poll_interval_seconds": max(float(poll_interval_seconds or 0.0), 0.0),
    }
    if not latest["database_runtime_available"] and not _safe_text(latest.get("error")):
        latest["error"] = (
            "Mongo read/write health was not stable across replay preflight samples."
        )
    return latest


def _normalised_ollama_model_names(payload: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raw_models = payload if isinstance(payload, list) else []
    for item in raw_models:
        if isinstance(item, Mapping):
            candidates = (item.get("name"), item.get("model"), item.get("id"))
        else:
            candidates = (item,)
        for candidate in candidates:
            name = _safe_text(candidate)
            if not name:
                continue
            names.add(name)
            if name.endswith(":latest"):
                names.add(name.removesuffix(":latest"))
            elif ":" not in name:
                names.add(f"{name}:latest")
    return names


def build_llm_preflight(
    *,
    session: requests.Session,
    base_url: str,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    explicit_model: str | None,
) -> dict[str, Any]:
    params: dict[str, str] = {}
    target_user = _safe_text(user_concept_id)
    target_org = _safe_text(organisation_concept_id)
    if target_user:
        params["user_concept_id"] = target_user
    if target_org:
        params["organisation_concept_id"] = target_org
    try:
        llm_info = _request_json(
            session,
            "GET",
            f"{base_url}/api/settings/llm/info",
            timeout_seconds=45.0,
            params=params,
        )
    except JsonRequestError as exc:
        return {
            "llm_runtime_available": False,
            "error": str(exc),
            "status_code": exc.status_code,
            "payload": exc.payload,
            "target_user_concept_id": target_user or None,
            "target_organisation_concept_id": target_org or None,
        }
    except Exception as exc:
        return {
            "llm_runtime_available": False,
            "error": str(exc),
            "target_user_concept_id": target_user or None,
            "target_organisation_concept_id": target_org or None,
        }

    provider = _safe_text(llm_info.get("provider")).lower()
    selected_model = _safe_text(llm_info.get("model"))
    model_override = _safe_text(explicit_model)
    model_to_check = model_override or selected_model
    preflight = {
        **dict(llm_info),
        "target_user_concept_id": target_user or None,
        "target_organisation_concept_id": target_org or None,
        "model_override": model_override or None,
        "model_checked": model_to_check or None,
        "selected_model_available": True,
        "llm_runtime_available": bool(
            llm_info.get("ping_ok") is True
            and _safe_text(llm_info.get("status")).lower() == "ready"
        ),
    }
    if provider == "ollama" and model_to_check:
        try:
            models_payload = _request_json(
                session,
                "GET",
                f"{base_url}/api/settings/ollama/models",
                timeout_seconds=45.0,
                params={"nocache": "1"},
            )
            available_model_names = _normalised_ollama_model_names(models_payload)
            preflight["available_ollama_model_names"] = sorted(available_model_names)[
                :200
            ]
            preflight["selected_model_available"] = (
                model_to_check in available_model_names
            )
        except JsonRequestError as exc:
            preflight["ollama_models_error"] = str(exc)
            preflight["ollama_models_status_code"] = exc.status_code
            preflight["selected_model_available"] = False
        except Exception as exc:
            preflight["ollama_models_error"] = str(exc)
            preflight["selected_model_available"] = False
    preflight["llm_runtime_available"] = bool(
        preflight["llm_runtime_available"]
        and preflight.get("selected_model_available") is not False
    )
    if not preflight["llm_runtime_available"] and not _safe_text(
        preflight.get("error")
    ):
        preflight["error"] = (
            "The effective replay model is not ready or not available for the "
            "target user/org context."
        )
    return preflight


def build_workflow_capability_preflight(
    *,
    session: requests.Session,
    base_url: str,
    timeout_seconds: float = 90.0,
    poll_interval_seconds: float = 1.5,
) -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    effective_timeout = max(float(timeout_seconds or 0.0), 0.0)
    deadline = time.monotonic() + effective_timeout
    attempt_count = 0
    last_preflight: dict[str, Any] = {}

    while True:
        attempt_count += 1
        try:
            payload = _request_json(
                session,
                "GET",
                f"{base_url}/api/workflows/capability-index/status",
                timeout_seconds=20.0,
            )
        except JsonRequestError as exc:
            payload = {
                "error": str(exc),
                "status_code": exc.status_code,
                "payload": exc.payload,
                "ready": False,
                "workflow_discovery_available": False,
            }
        except Exception as exc:
            payload = {
                "error": str(exc),
                "ready": False,
                "workflow_discovery_available": False,
            }

        preflight = dict(payload)
        if "workflow_discovery_available" not in preflight:
            preflight["workflow_discovery_available"] = payload.get("ready") is True
        last_preflight = preflight
        namespace_state = _as_mapping(preflight.get("namespace_state"))
        status = _safe_text(preflight.get("status")).lower()
        snapshots.append(
            {
                "attempt": attempt_count,
                "status": preflight.get("status"),
                "ready": preflight.get("ready"),
                "workflow_discovery_available": preflight.get(
                    "workflow_discovery_available"
                ),
                "build_in_progress": preflight.get("build_in_progress"),
                "size": preflight.get("size"),
                "last_manifest_status": preflight.get("last_manifest_status"),
                "last_invalidation_reason": preflight.get("last_invalidation_reason"),
                "namespace_status": namespace_state.get("status"),
                "detail": preflight.get("detail"),
            }
        )
        if preflight.get("workflow_discovery_available") is True:
            break

        transient = bool(preflight.get("build_in_progress")) or status in {
            "building",
            "rebuilding",
            "warming",
        }
        if _safe_text(namespace_state.get("status")).lower() == "rebuild_in_progress":
            transient = True
        if not transient:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(max(float(poll_interval_seconds or 0.0), 0.2))

    last_preflight["preflight_wait"] = {
        "timeout_seconds": effective_timeout,
        "poll_interval_seconds": max(float(poll_interval_seconds or 0.0), 0.2),
        "attempt_count": attempt_count,
        "completed": last_preflight.get("workflow_discovery_available") is True,
        "timed_out": (
            last_preflight.get("workflow_discovery_available") is not True
            and time.monotonic() >= deadline
        ),
        "snapshots": snapshots[-8:],
    }
    return last_preflight


def submit_background_generate(
    *,
    session: requests.Session,
    base_url: str,
    case: ReplayCase,
    client_request_id: str,
    conversation_session_id: str | None,
    gmail_profile: str | None,
    model: str | None,
    presenter_mode: bool,
    thinking_card_mode: str,
    agent_test_selector_replay_mode: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prompt": case.prompt,
        "background": True,
        "client_request_id": client_request_id,
        "thinking_card_mode": thinking_card_mode,
    }
    if conversation_session_id:
        payload["conversation_session_id"] = conversation_session_id
    if gmail_profile:
        payload["gmail_profile"] = gmail_profile
    if model:
        payload["model"] = model
    if presenter_mode:
        payload["presenter_mode"] = True
    if agent_test_selector_replay_mode and agent_test_selector_replay_mode.strip():
        payload[AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY] = (
            agent_test_selector_replay_mode.strip()
        )
    if isinstance(case.workflow_inputs, Mapping) and case.workflow_inputs:
        payload["workflow_inputs"] = {
            str(key): value
            for key, value in case.workflow_inputs.items()
            if isinstance(key, str) and key.strip()
        }

    return _request_json(
        session,
        "POST",
        f"{base_url}/von/generate",
        expected_status=202,
        timeout_seconds=45.0,
        json=payload,
    )


def poll_replay_task(
    *,
    session: requests.Session,
    base_url: str,
    task_id: str,
    request_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    cancel_on_timeout: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    task_statuses: list[dict[str, Any]] = []
    progress_snapshots: list[dict[str, Any]] = []
    last_task_status: dict[str, Any] = {}
    last_progress: dict[str, Any] = {}

    while time.monotonic() <= deadline:
        try:
            last_task_status = _request_json(
                session,
                "GET",
                f"{base_url}/von/api/task/status/{task_id}",
                timeout_seconds=20.0,
            )
            task_statuses.append(last_task_status)
        except Exception as exc:
            last_task_status = {"error": str(exc)}
            task_statuses.append(last_task_status)

        try:
            last_progress = _request_json(
                session,
                "GET",
                f"{base_url}/von/progress/{request_id}",
                expected_status=(200, 202),
                timeout_seconds=20.0,
            )
            progress_snapshots.append(last_progress)
        except Exception as exc:
            progress_snapshots.append({"error": str(exc)})

        status = _safe_text(last_task_status.get("status"))
        if status in TERMINAL_TASK_STATUSES:
            break
        time.sleep(max(float(poll_interval_seconds), 0.2))

    task_result: dict[str, Any] = {}
    if _safe_text(last_task_status.get("status")) == "completed":
        task_result = _request_json(
            session,
            "GET",
            f"{base_url}/von/api/task/result/{task_id}",
            timeout_seconds=45.0,
        )
    elif _safe_text(last_task_status.get("status")) not in TERMINAL_TASK_STATUSES:
        if not cancel_on_timeout:
            task_result = {
                "timeout_without_cancellation": True,
                "message": (
                    "Replay timeout reached; cancellation was skipped by "
                    "harness option."
                ),
                "task_id": task_id,
            }
            return {
                "task_statuses": task_statuses,
                "progress_snapshots": progress_snapshots,
                "last_task_status": last_task_status,
                "last_progress": last_progress,
                "task_result": task_result,
            }
        try:
            task_result = _request_json(
                session,
                "POST",
                f"{base_url}/von/api/task/cancel/{task_id}",
                timeout_seconds=20.0,
            )
        except Exception as exc:
            task_result = {"cancellation_error": str(exc)}
        try:
            refreshed_status = _request_json(
                session,
                "GET",
                f"{base_url}/von/api/task/status/{task_id}",
                timeout_seconds=20.0,
            )
            last_task_status = refreshed_status
            task_statuses.append(refreshed_status)
        except Exception as exc:
            task_result["post_cancellation_status_error"] = str(exc)

    return {
        "task_statuses": task_statuses,
        "progress_snapshots": progress_snapshots,
        "last_task_status": last_task_status,
        "last_progress": last_progress,
        "task_result": task_result,
    }


def _walk_json(value: Any) -> list[Any]:
    values = [value]
    if isinstance(value, Mapping):
        for child in value.values():
            values.extend(_walk_json(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(_walk_json(child))
    return values


def extract_progress_facts(*payloads: Any) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        for value in _walk_json(payload):
            if not isinstance(value, Mapping):
                continue
            candidate = value.get("progress_facts")
            if not isinstance(candidate, list):
                continue
            for raw_fact in candidate:
                if not isinstance(raw_fact, Mapping):
                    continue
                fact = {str(key): item for key, item in raw_fact.items()}
                identity = json.dumps(fact, sort_keys=True, ensure_ascii=True)
                if identity in seen:
                    continue
                seen.add(identity)
                facts.append(fact)
    return facts


def extract_selected_workflow_ids(*payloads: Any) -> list[str]:
    workflow_ids: list[str] = []
    for payload in payloads:
        for value in _walk_json(payload):
            if not isinstance(value, Mapping):
                continue
            for key in ("selected_workflow_id", "dispatch_workflow_id"):
                workflow_id = _safe_text(value.get(key))
                if workflow_id and workflow_id not in workflow_ids:
                    workflow_ids.append(workflow_id)
    return workflow_ids


def extract_observed_workflow_ids(*payloads: Any) -> list[str]:
    workflow_ids: list[str] = []
    for payload in payloads:
        for value in _walk_json(payload):
            if not isinstance(value, Mapping):
                continue
            workflow_id = _safe_text(value.get("workflow_id"))
            if (
                workflow_id.startswith("#V#")
                and workflow_id not in workflow_ids
            ):
                workflow_ids.append(workflow_id)
    return workflow_ids


def extract_selector_diagnostics(*payloads: Any) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    seen: set[str] = set()
    diagnostic_keys = (
        "workflow_selection",
        "workflow_routing_diagnostics",
        "selected_workflow_execution",
        "workflow_stage_path",
    )
    scalar_keys = (
        "selected_workflow_id",
        "dispatch_workflow_id",
        "workflow_id",
        "state_id",
        "phase",
        "status",
        "selection_resolution",
        "verdict",
    )
    list_keys = (
        "discovered_workflow_ids",
        "candidate_workflow_ids",
        "eligible_specialised_candidate_ids",
        "selector_candidate_ids",
        "selector_discovered_workflow_ids",
        "selector_excluded_candidate_ids",
    )
    for payload in payloads:
        for value in _walk_json(payload):
            if not isinstance(value, Mapping):
                continue
            entry: dict[str, Any] = {}
            for key in scalar_keys:
                text = _safe_text(value.get(key))
                if text:
                    entry[key] = text
            for key in list_keys:
                raw_items = value.get(key)
                if isinstance(raw_items, list):
                    items = [_safe_text(item) for item in raw_items]
                    entry[key] = [item for item in items if item][:20]
            for key in diagnostic_keys:
                raw_value = value.get(key)
                if isinstance(raw_value, Mapping):
                    nested_entry: dict[str, Any] = {}
                    for nested_key in scalar_keys:
                        text = _safe_text(raw_value.get(nested_key))
                        if text:
                            nested_entry[nested_key] = text
                    for nested_key in list_keys:
                        raw_items = raw_value.get(nested_key)
                        if isinstance(raw_items, list):
                            items = [_safe_text(item) for item in raw_items]
                            nested_entry[nested_key] = [item for item in items if item][
                                :20
                            ]
                    if nested_entry:
                        entry[key] = nested_entry
            if not entry:
                continue
            identity = json.dumps(entry, sort_keys=True, ensure_ascii=True)
            if identity in seen:
                continue
            seen.add(identity)
            diagnostics.append(entry)
    return diagnostics[:80]


def extract_visible_answer(task_result: Mapping[str, Any]) -> str | None:
    result = _as_mapping(task_result.get("result"))
    response_text = _safe_text(result.get("response_text"))
    if response_text:
        return response_text
    llm_debug = _as_mapping(result.get("llm_debug"))
    debug_response = _safe_text(llm_debug.get("response"))
    return debug_response or None


_ARXIV_IDENTIFIER_PATTERN = re.compile(
    r"(?i)(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)?"
    r"(?P<id>\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?"
)


def _append_unique(items: list[str], value: Any) -> None:
    text = _safe_text(value)
    if text and text not in items:
        items.append(text)


def _iter_scalar_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_safe_text(value)] if _safe_text(value) else []
    if isinstance(value, (int, float, bool)):
        return [_safe_text(value)]
    if isinstance(value, Mapping):
        texts: list[str] = []
        for item in value.values():
            texts.extend(_iter_scalar_texts(item))
        return texts
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        texts = []
        for item in value:
            texts.extend(_iter_scalar_texts(item))
        return texts
    return []


def _collect_keyed_scalar_values(
    value: Any,
    *,
    exact_keys: set[str] | None = None,
    key_markers: tuple[str, ...] = (),
) -> list[str]:
    exact = {item.lower() for item in (exact_keys or set())}
    results: list[str] = []
    for candidate in _walk_json(value):
        if not isinstance(candidate, Mapping):
            continue
        for raw_key, raw_value in candidate.items():
            key = _safe_text(raw_key).lower()
            if not key:
                continue
            if key not in exact and not any(marker in key for marker in key_markers):
                continue
            for text in _iter_scalar_texts(raw_value):
                _append_unique(results, text)
    return results


def _normalise_arxiv_identifier(value: Any) -> str | None:
    text = _safe_text(value)
    if not text:
        return None
    match = _ARXIV_IDENTIFIER_PATTERN.search(text)
    if not match:
        return None
    return match.group("id")


def _extract_arxiv_identifiers(value: Any) -> list[str]:
    identifiers: list[str] = []
    for raw in _collect_keyed_scalar_values(value, key_markers=("arxiv",)):
        identifier = _normalise_arxiv_identifier(raw)
        if identifier:
            _append_unique(identifiers, identifier)
    for fact in extract_progress_facts(value):
        fact_id = _safe_text(fact.get("fact_id")) or _safe_text(fact.get("id"))
        if "arxiv" not in fact_id.lower():
            continue
        for key in ("value", "raw_value", "display_value", "text", "label"):
            identifier = _normalise_arxiv_identifier(fact.get(key))
            if identifier:
                _append_unique(identifiers, identifier)
    return identifiers


def _extract_progress_fact_values(
    value: Any,
    *,
    fact_ids: set[str],
) -> list[str]:
    results: list[str] = []
    wanted = {item.lower() for item in fact_ids}
    for fact in extract_progress_facts(value):
        fact_id = (
            _safe_text(fact.get("fact_id")) or _safe_text(fact.get("id"))
        ).lower()
        if fact_id not in wanted:
            continue
        for key in ("value", "raw_value", "display_value", "text", "concept_id"):
            for text in _iter_scalar_texts(fact.get(key)):
                _append_unique(results, text)
    return results


def extract_observed_tool_names(*payloads: Any) -> list[str]:
    tools: list[str] = []
    for payload in payloads:
        for raw in _collect_keyed_scalar_values(
            payload,
            exact_keys={
                "tool",
                "tool_name",
                "mcp_tool",
                "mcp_requested_tool",
                "mcp_resolved_tool",
            },
        ):
            _append_unique(tools, raw)
    return tools


def _mapping_mentions_tool(value: Mapping[str, Any], tool_name: str) -> bool:
    wanted = _safe_text(tool_name)
    if not wanted:
        return False
    return wanted in extract_observed_tool_names(value)


MESSAGE_PROCESSING_MARKER_KEYS = {
    "gmail_message_processing_marker",
    "gmail_processing_status",
    "message_processed",
    "message_processed_for_paper",
    "message_processing_marker",
    "message_processing_status",
    "processed_gmail_message_id",
    "processed_message_id",
    "source_message_processed",
}
MESSAGE_PROCESSING_MARKER_MESSAGE_ID_KEYS = {
    "processed_gmail_message_id",
    "processed_message_id",
}


def _extract_gmail_mutation_tools(value: Any) -> list[str]:
    tools: list[str] = []
    for tool_name in extract_observed_tool_names(value):
        if tool_name in GMAIL_MUTATION_TOOL_NAMES:
            _append_unique(tools, tool_name)
    return tools


def _extract_gmail_mutation_message_ids(value: Any) -> list[str]:
    message_ids: list[str] = []
    for candidate in _walk_json(value):
        if not isinstance(candidate, Mapping):
            continue
        if not any(
            _mapping_mentions_tool(candidate, tool_name)
            for tool_name in GMAIL_MUTATION_TOOL_NAMES
        ):
            continue
        for message_id in _collect_keyed_scalar_values(
            candidate,
            exact_keys={"message_id", "gmail_message_id"},
        ):
            _append_unique(message_ids, message_id)
    return message_ids


def _is_truthy_marker_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _safe_text(value).lower()
    return bool(text) and text not in {
        "0",
        "false",
        "none",
        "not_processed",
        "null",
        "unprocessed",
    }


def _mapping_has_message_processing_marker(value: Mapping[str, Any]) -> bool:
    for raw_key, raw_value in value.items():
        if _safe_text(raw_key).lower() in MESSAGE_PROCESSING_MARKER_KEYS:
            if _is_truthy_marker_value(raw_value):
                return True
    return False


def _extract_message_processing_marker_values(value: Any) -> list[str]:
    markers: list[str] = []
    for raw in _collect_keyed_scalar_values(
        value,
        exact_keys=MESSAGE_PROCESSING_MARKER_KEYS,
    ):
        if _is_truthy_marker_value(raw):
            _append_unique(markers, raw)
    return markers


def _extract_message_processing_marker_message_ids(value: Any) -> list[str]:
    message_ids: list[str] = []
    for marker_value in _collect_keyed_scalar_values(
        value,
        exact_keys=MESSAGE_PROCESSING_MARKER_MESSAGE_ID_KEYS,
    ):
        _append_unique(message_ids, marker_value)
    for candidate in _walk_json(value):
        if not isinstance(candidate, Mapping):
            continue
        if not _mapping_has_message_processing_marker(candidate):
            continue
        for message_id in _collect_keyed_scalar_values(
            candidate,
            exact_keys={"message_id", "gmail_message_id"},
        ):
            _append_unique(message_ids, message_id)
    return message_ids


def extract_gmail_arxiv_idempotence_evidence(*payloads: Any) -> dict[str, Any]:
    """Extract privacy-bounded idempotence evidence from replay telemetry."""

    evidence: dict[str, Any] = {
        "message_ids": [],
        "thread_ids": [],
        "arxiv_ids": [],
        "paper_concept_ids": [],
        "file_copy_concept_ids": [],
        "observed_tools": [],
        "gmail_mutation_tools": [],
        "gmail_mutation_message_ids": [],
        "message_processing_marker_values": [],
        "message_processing_marker_message_ids": [],
        "effective_gmail_queries": [],
    }
    for payload in payloads:
        for value in _collect_keyed_scalar_values(
            payload,
            exact_keys={"message_id", "gmail_message_id"},
        ):
            _append_unique(evidence["message_ids"], value)
        for value in _collect_keyed_scalar_values(
            payload,
            exact_keys={"thread_id", "gmail_thread_id"},
        ):
            _append_unique(evidence["thread_ids"], value)
        for value in _extract_arxiv_identifiers(payload):
            _append_unique(evidence["arxiv_ids"], value)
        for value in _collect_keyed_scalar_values(
            payload,
            exact_keys={"paper_concept_id"},
        ):
            _append_unique(evidence["paper_concept_ids"], value)
        for value in _extract_progress_fact_values(
            payload,
            fact_ids={"paper_concept", "arxiv_paper_concept"},
        ):
            if value.startswith("#V#"):
                _append_unique(evidence["paper_concept_ids"], value)
        for value in _collect_keyed_scalar_values(
            payload,
            exact_keys={"file_copy_concept_id"},
        ):
            _append_unique(evidence["file_copy_concept_ids"], value)
        for value in extract_observed_tool_names(payload):
            _append_unique(evidence["observed_tools"], value)
        for value in _extract_gmail_mutation_tools(payload):
            _append_unique(evidence["gmail_mutation_tools"], value)
        for value in _extract_gmail_mutation_message_ids(payload):
            _append_unique(evidence["gmail_mutation_message_ids"], value)
        for value in _extract_message_processing_marker_values(payload):
            _append_unique(evidence["message_processing_marker_values"], value)
        for value in _extract_message_processing_marker_message_ids(payload):
            _append_unique(evidence["message_processing_marker_message_ids"], value)
        for value in _collect_keyed_scalar_values(
            payload,
            exact_keys={"effective_gmail_query", "effective_query"},
        ):
            _append_unique(evidence["effective_gmail_queries"], value)
    evidence["message_processing_marker_seen"] = bool(
        evidence["message_processing_marker_values"]
        or evidence["message_processing_marker_message_ids"]
    )
    return evidence


def _turn_report_status(report: Mapping[str, Any]) -> str:
    analysis = _as_mapping(report.get("analysis"))
    status = _safe_text(analysis.get("terminal_task_status"))
    if status:
        return status
    return _safe_text(_as_mapping(report.get("last_task_status")).get("status"))


def _blocked_sequence_analysis(
    blocker_type: str,
    reason: str,
    *,
    evidence: Mapping[str, Any] | None = None,
    turn_reports: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "verdict": "blocked",
        "blocker": {
            "type": blocker_type,
            "reason": reason,
        },
        "evidence": dict(evidence or {}),
        "turn_count": len(turn_reports),
    }


def analyse_gmail_arxiv_idempotence_sequence(
    turn_reports: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify the 2571 first/repeat/verify replay without adding policy."""

    reports = [dict(report) for report in turn_reports if isinstance(report, Mapping)]
    if len(reports) < 3:
        return _blocked_sequence_analysis(
            "sequence_incomplete",
            "The replay did not submit all three required turns.",
            turn_reports=reports,
        )

    per_turn: dict[str, dict[str, Any]] = {
        _safe_text(report.get("turn_id")) or f"turn_{index + 1}": report
        for index, report in enumerate(reports)
    }
    for turn_id, report in per_turn.items():
        analysis = _as_mapping(report.get("analysis"))
        if _safe_text(analysis.get("verdict")) == "blocked":
            blocker = _as_mapping(analysis.get("blocker"))
            blocker_type = _safe_text(blocker.get("type")) or "turn_blocked"
            return _blocked_sequence_analysis(
                blocker_type,
                f"Turn {turn_id!r} was blocked: "
                f"{_safe_text(blocker.get('reason')) or blocker_type}.",
                evidence={"blocked_turn_id": turn_id, "turn_analysis": analysis},
                turn_reports=reports,
            )
        terminal_status = _turn_report_status(report)
        if terminal_status and terminal_status != "completed":
            return _blocked_sequence_analysis(
                "turn_not_completed",
                f"Turn {turn_id!r} ended with status {terminal_status!r}.",
                evidence={
                    "blocked_turn_id": turn_id,
                    "terminal_status": terminal_status,
                },
                turn_reports=reports,
            )

    initial_report = per_turn.get("initial") or reports[0]
    repeat_report = per_turn.get("repeat") or reports[1]
    verify_report = per_turn.get("verify") or reports[2]
    initial = extract_gmail_arxiv_idempotence_evidence(initial_report)
    repeat = extract_gmail_arxiv_idempotence_evidence(repeat_report)
    verify = extract_gmail_arxiv_idempotence_evidence(verify_report)
    all_evidence = {
        "initial": initial,
        "repeat": repeat,
        "verify": verify,
    }
    gmail_mutation_tools = sorted(
        set(initial["gmail_mutation_tools"])
        | set(repeat["gmail_mutation_tools"])
        | set(verify["gmail_mutation_tools"])
    )
    if gmail_mutation_tools:
        return _blocked_sequence_analysis(
            "gmail_write_tool_used",
            "The read-only Gmail replay observed Gmail write-capable tools: "
            + ", ".join(gmail_mutation_tools),
            evidence=all_evidence,
            turn_reports=reports,
        )

    initial_selected = set(
        _as_list(
            _as_mapping(initial_report.get("analysis")).get("selected_workflow_ids")
        )
    )
    if GMAIL_ARXIV_WORKFLOW_ID not in initial_selected:
        return _blocked_sequence_analysis(
            "initial_workflow_not_selected",
            f"The initial turn did not select {GMAIL_ARXIV_WORKFLOW_ID}.",
            evidence=all_evidence,
            turn_reports=reports,
        )

    if len(initial["message_ids"]) != 1:
        return _blocked_sequence_analysis(
            "gmail_message_target_ambiguous",
            "The initial turn did not expose exactly one Gmail message id for the target.",
            evidence=all_evidence,
            turn_reports=reports,
        )
    if len(initial["arxiv_ids"]) != 1:
        return _blocked_sequence_analysis(
            "arxiv_target_ambiguous",
            "The initial turn did not expose exactly one arXiv id for the target.",
            evidence=all_evidence,
            turn_reports=reports,
        )
    if len(initial["paper_concept_ids"]) != 1:
        return _blocked_sequence_analysis(
            "paper_concept_readback_missing",
            "The initial turn did not expose exactly one represented paper concept id.",
            evidence=all_evidence,
            turn_reports=reports,
        )

    target_message_id = initial["message_ids"][0]
    target_arxiv_id = initial["arxiv_ids"][0]
    target_paper_concept_id = initial["paper_concept_ids"][0]
    post_paper_ids = set(repeat["paper_concept_ids"]) | set(verify["paper_concept_ids"])
    if target_paper_concept_id not in post_paper_ids:
        return _blocked_sequence_analysis(
            "repeat_readback_missing",
            "The repeat/verification turns did not read back the initial paper concept id.",
            evidence=all_evidence,
            turn_reports=reports,
        )

    all_paper_ids = (
        set(initial["paper_concept_ids"])
        | set(repeat["paper_concept_ids"])
        | set(verify["paper_concept_ids"])
    )
    if len(all_paper_ids) > 1:
        return _blocked_sequence_analysis(
            "duplicate_paper_concepts_observed",
            "The replay observed more than one paper concept id for the same target.",
            evidence=all_evidence,
            turn_reports=reports,
        )

    all_file_copy_ids = (
        set(initial["file_copy_concept_ids"])
        | set(repeat["file_copy_concept_ids"])
        | set(verify["file_copy_concept_ids"])
    )
    if len(all_file_copy_ids) > 1:
        return _blocked_sequence_analysis(
            "duplicate_file_copies_observed",
            "The replay observed more than one file-copy/import id for the same target.",
            evidence=all_evidence,
            turn_reports=reports,
        )

    if (
        target_message_id not in initial["message_processing_marker_message_ids"]
        or not initial["message_processing_marker_seen"]
    ):
        return _blocked_sequence_analysis(
            "message_processing_marker_missing",
            "The initial turn did not prove the source Gmail message had represented processing evidence.",
            evidence=all_evidence,
            turn_reports=reports,
        )

    post_marker_readback = bool(
        repeat["message_processing_marker_seen"]
        or verify["message_processing_marker_seen"]
        or any(
            message_id == target_message_id
            for message_id in (
                repeat["message_processing_marker_message_ids"]
                + verify["message_processing_marker_message_ids"]
            )
        )
    )
    if not post_marker_readback:
        return _blocked_sequence_analysis(
            "message_processing_readback_missing",
            "The repeat/verification turns did not read back the message-processing marker.",
            evidence=all_evidence,
            turn_reports=reports,
        )

    warnings: list[str] = []
    if not all_file_copy_ids:
        warnings.append("file_copy_or_import_id_not_observed")
    return {
        "verdict": "pass",
        "blocker": None,
        "turn_count": len(reports),
        "target": {
            "gmail_message_id": target_message_id,
            "gmail_thread_ids": initial["thread_ids"],
            "arxiv_id": target_arxiv_id,
            "paper_concept_id": target_paper_concept_id,
            "file_copy_concept_ids": sorted(all_file_copy_ids),
        },
        "evidence": all_evidence,
        "warnings": warnings,
    }


def _fact_ids(facts: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        _safe_text(fact.get("fact_id")) or _safe_text(fact.get("id"))
        for fact in facts
        if _safe_text(fact.get("fact_id")) or _safe_text(fact.get("id"))
    }


def _contract_ids(facts: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        _safe_text(fact.get("contract_id"))
        for fact in facts
        if _safe_text(fact.get("contract_id"))
    }


_GMAIL_OAUTH_AUTH_MARKERS = (
    "oauth",
    "token",
    "credential",
    "authoris",
    "authoriz",
    "profile",
)
_GMAIL_OAUTH_FAILURE_MARKERS = (
    "denied",
    "disabled",
    "disconnected",
    "expired",
    "failed",
    "failure",
    "invalid",
    "missing",
    "not authenticated",
    "not configured",
    "not connected",
    "not ready",
    "reauth",
    "re-auth",
    "revoked",
    "unavailable",
)
_GMAIL_CONTEXT_KEYS = (
    "gmail",
    "gmail_preflight",
    "gmail_profile",
    "requested_gmail_profile",
)
_GMAIL_IDENTIFIER_KEYS = {
    "action_id",
    "concept_id",
    "contract_id",
    "fact_id",
    "id",
    "selected_workflow_id",
    "state_id",
    "workflow_id",
}
_GMAIL_OAUTH_NON_EVIDENCE_KEYS = {
    "available_tools",
    "context_message",
    "context_messages",
    "llm_prompt",
    "llm_request",
    "messages",
    "prompt",
    "prompt_preview",
    "request",
    "selector_prompt",
    "system_prompt",
    "tool_specs",
}
_TOOL_REGISTRATION_NON_EVIDENCE_KEYS = {
    "available_tools",
    "context_message",
    "context_messages",
    "llm_prompt",
    "llm_request",
    "messages",
    "prompt",
    "prompt_preview",
    "request",
    "selector_prompt",
    "system_prompt",
    "tool_specs",
}
_TOOL_REGISTRATION_CONTEXT_MARKERS = (
    "mcp",
    "method",
    "tool",
    "workflow",
)


def _text_mentions_gmail(value: str) -> bool:
    text = value.lower()
    return "gmail" in text or "google mail" in text


def _text_mentions_gmail_oauth_failure(value: str, *, gmail_context: bool) -> bool:
    text = value.lower()
    gmailish = gmail_context or _text_mentions_gmail(text)
    if not gmailish:
        return False
    authish = any(marker in text for marker in _GMAIL_OAUTH_AUTH_MARKERS)
    failureish = any(marker in text for marker in _GMAIL_OAUTH_FAILURE_MARKERS)
    return authish and failureish


def _text_mentions_tool_registration_failure(value: str) -> bool:
    text = value.lower()
    if "mcp_invoke_failed" in text or "method '" in text and "not registered" in text:
        return True
    if "not registered" not in text:
        return False
    return any(marker in text for marker in _TOOL_REGISTRATION_CONTEXT_MARKERS)


def _contains_tool_registration_blocker_text(value: Any) -> bool:
    if isinstance(value, str):
        return _text_mentions_tool_registration_failure(value)
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = _safe_text(raw_key).lower()
            if key in _TOOL_REGISTRATION_NON_EVIDENCE_KEYS:
                continue
            if _contains_tool_registration_blocker_text(item):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_tool_registration_blocker_text(item) for item in value)
    return False


def _mapping_has_gmail_context(value: Mapping[str, Any]) -> bool:
    for raw_key, raw_item in value.items():
        key = _safe_text(raw_key).lower()
        if key in _GMAIL_IDENTIFIER_KEYS:
            continue
        if any(marker in key for marker in _GMAIL_CONTEXT_KEYS):
            return True
        if isinstance(raw_item, str) and _text_mentions_gmail(raw_item):
            return True
    return False


def _contains_gmail_oauth_blocker_text(
    value: Any,
    *,
    gmail_context: bool = False,
) -> bool:
    if isinstance(value, str):
        return _text_mentions_gmail_oauth_failure(value, gmail_context=gmail_context)
    if isinstance(value, Mapping):
        local_gmail_context = gmail_context or _mapping_has_gmail_context(value)
        for raw_key, item in value.items():
            key = _safe_text(raw_key).lower()
            if key in _GMAIL_OAUTH_NON_EVIDENCE_KEYS:
                continue
            child_gmail_context = local_gmail_context
            if key in _GMAIL_IDENTIFIER_KEYS:
                child_gmail_context = gmail_context
            elif any(marker in key for marker in _GMAIL_CONTEXT_KEYS):
                child_gmail_context = True
            if _contains_gmail_oauth_blocker_text(
                item,
                gmail_context=child_gmail_context,
            ):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(
            _contains_gmail_oauth_blocker_text(
                item,
                gmail_context=gmail_context,
            )
            for item in value
        )
    return False


def _selector_route_evidence_present(
    selected_workflow_ids: Sequence[str],
    observed_workflow_ids: Sequence[str],
    selector_diagnostics: Sequence[Mapping[str, Any]],
) -> bool:
    if selected_workflow_ids or observed_workflow_ids:
        return True
    route_keys = {
        "selected_workflow_id",
        "dispatch_workflow_id",
        "workflow_id",
        "workflow_selection",
        "workflow_routing_diagnostics",
        "selected_workflow_execution",
        "discovered_workflow_ids",
        "candidate_workflow_ids",
        "eligible_specialised_candidate_ids",
        "selector_candidate_ids",
        "selector_discovered_workflow_ids",
        "selector_excluded_candidate_ids",
    }
    for item in selector_diagnostics:
        if any(key in item for key in route_keys):
            return True
    return False


def _context_build_progress_blocker(
    *,
    terminal_status: str,
    last_task_status: Mapping[str, Any],
    selector_diagnostics: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    progress = _as_mapping(last_task_status.get("progress"))
    observed_stage_values = {
        _safe_text(progress.get("stage")),
        _safe_text(progress.get("phase")),
        _safe_text(progress.get("workflow_task")),
    }
    for item in selector_diagnostics:
        observed_stage_values.add(_safe_text(item.get("stage")))
        observed_stage_values.add(_safe_text(item.get("phase")))
        observed_stage_values.add(_safe_text(item.get("workflow_task")))
    observed_stage_values.discard("")
    if "context_build" not in observed_stage_values:
        return None

    return {
        "type": "context_build_blocker",
        "reason": (
            "The background turn did not reach workflow selection before it "
            f"ended with status {terminal_status!r}; the last visible stage "
            "was context_build/request setup, so no selector candidate set was "
            "presented to the selector LLM in this replay."
        ),
        "terminal_task_status": terminal_status,
        "last_task_status": dict(last_task_status),
        "selector_diagnostics": [dict(item) for item in selector_diagnostics],
    }


def _terminal_workflow_action_blocker(
    *,
    terminal_status: str,
    last_task_status: Mapping[str, Any],
    selected_workflow_ids: Sequence[str],
    observed_workflow_ids: Sequence[str],
    missing_fact_ids: Sequence[str],
    missing_contract_ids: Sequence[str],
) -> dict[str, Any]:
    progress = _as_mapping(last_task_status.get("progress"))
    workflow_id = _safe_text(progress.get("workflow_id")) or None
    state_id = _safe_text(progress.get("state_id")) or None
    action_id = _safe_text(progress.get("action_id")) or None
    phase = _safe_text(progress.get("phase")) or None
    reason_parts = [f"Background task ended with status {terminal_status!r}"]
    if workflow_id or state_id or action_id:
        location = "/".join(item for item in (workflow_id, state_id, action_id) if item)
        reason_parts.append(f"after reaching {location}")
    return {
        "type": "workflow_action_terminal_state_blocker",
        "reason": "; ".join(reason_parts) + ".",
        "terminal_task_status": terminal_status,
        "last_workflow_id": workflow_id,
        "last_state_id": state_id,
        "last_action_id": action_id,
        "last_phase": phase,
        "selected_workflow_ids": list(selected_workflow_ids),
        "observed_workflow_ids": list(observed_workflow_ids),
        "missing_progress_fact_ids": list(missing_fact_ids),
        "missing_contract_ids": list(missing_contract_ids),
        "last_task_status": dict(last_task_status),
    }


def build_precondition_blockers(
    *,
    auth_login: Mapping[str, Any],
    auth_status: Mapping[str, Any],
    target_session_context: Mapping[str, Any] | None = None,
    database_preflight: Mapping[str, Any] | None = None,
    llm_preflight: Mapping[str, Any] | None = None,
    workflow_capability_preflight: Mapping[str, Any] | None = None,
    gmail_preflight: Mapping[str, Any] | None = None,
    workflow_capability_forced: bool = False,
    database_forced: bool = False,
    llm_forced: bool = False,
    gmail_forced: bool = False,
) -> list[dict[str, Any]]:
    """Return explicit replay infrastructure blockers.

    These are operational preconditions for a user-equivalent replay.  Keeping
    them as a list prevents one infrastructure failure from hiding another.
    """

    blockers: list[dict[str, Any]] = []
    target_context = dict(
        target_session_context
        if isinstance(target_session_context, Mapping)
        else {"target_session_context_ready": True}
    )
    verified_target_context_ready = bool(
        isinstance(target_session_context, Mapping)
        and target_context.get("target_session_context_ready") is True
    )
    auth_ready = bool(
        auth_login.get("success")
        or auth_status.get("authenticated")
        or verified_target_context_ready
    )
    if not auth_ready:
        blockers.append(
            {
                "type": "browser_test_auth_blocker",
                "reason": _safe_text(auth_login.get("error"))
                or _safe_text(auth_status.get("error"))
                or "No authenticated browser-test or trusted local replay context was established.",
            }
        )

    if target_context.get("target_session_context_ready") is False:
        blockers.append(
            {
                "type": "target_session_context_blocker",
                "reason": _safe_text(target_context.get("error"))
                or "; ".join(
                    _safe_text(item)
                    for item in _as_list(target_context.get("mismatch_reasons"))
                    if _safe_text(item)
                )
                or "Replay session did not resolve to the requested user/org context.",
                "target_session_context": target_context,
            }
        )

    db_preflight = dict(
        database_preflight
        if isinstance(database_preflight, Mapping)
        else {"database_runtime_available": True}
    )
    if (
        db_preflight.get("database_runtime_available") is not True
        and not database_forced
    ):
        blockers.append(
            {
                "type": "database_runtime_blocker",
                "reason": _safe_text(db_preflight.get("error"))
                or "Mongo read/write health is not stable enough for replay.",
                "database_preflight": db_preflight,
            }
        )

    llm_payload = dict(
        llm_preflight
        if isinstance(llm_preflight, Mapping)
        else {"llm_runtime_available": True}
    )
    if llm_payload.get("llm_runtime_available") is not True and not llm_forced:
        blockers.append(
            {
                "type": "llm_runtime_blocker",
                "reason": _safe_text(llm_payload.get("error"))
                or "The effective replay LLM is not ready for the target user/org.",
                "llm_preflight": llm_payload,
            }
        )

    workflow_preflight = dict(
        workflow_capability_preflight
        if isinstance(workflow_capability_preflight, Mapping)
        else {"workflow_discovery_available": True}
    )
    if (
        workflow_preflight.get("workflow_discovery_available") is not True
        and not workflow_capability_forced
    ):
        blockers.append(
            {
                "type": "workflow_capability_index_blocker",
                "reason": _safe_text(workflow_preflight.get("detail"))
                or _safe_text(workflow_preflight.get("summary"))
                or _safe_text(workflow_preflight.get("error"))
                or (
                    "Workflow discovery cannot rely on the authoritative "
                    "capability index."
                ),
                "workflow_capability_preflight": workflow_preflight,
            }
        )

    gmail_payload = dict(
        gmail_preflight if isinstance(gmail_preflight, Mapping) else {}
    )
    if not gmail_payload.get("gmail_capability_ready") and not gmail_forced:
        access_test = _as_mapping(gmail_payload.get("access_test"))
        gmail_reason = (
            _safe_text(access_test.get("detail"))
            or _safe_text(access_test.get("error"))
            or _safe_text(access_test.get("status"))
        )
        blockers.append(
            {
                "type": "gmail_oauth_or_profile_blocker",
                "reason": gmail_reason
                or (
                    "Gmail profile/tokens are not ready for an authenticated "
                    "Gmail-backed replay."
                ),
                "gmail_preflight": gmail_payload,
            }
        )
    return blockers


def classify_replay(
    *,
    case: ReplayCase,
    auth_login: Mapping[str, Any],
    auth_status: Mapping[str, Any],
    target_session_context: Mapping[str, Any] | None = None,
    database_preflight: Mapping[str, Any] | None = None,
    llm_preflight: Mapping[str, Any] | None = None,
    gmail_preflight: Mapping[str, Any],
    task_evidence: Mapping[str, Any],
    selected_workflow_ids: Sequence[str],
    observed_workflow_ids: Sequence[str],
    selector_diagnostics: Sequence[Mapping[str, Any]],
    progress_facts: Sequence[Mapping[str, Any]],
    workflow_capability_preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    missing_fact_ids = sorted(
        set(case.expected_progress_fact_ids) - _fact_ids(progress_facts)
    )
    missing_contract_ids = sorted(
        set(case.expected_contract_ids) - _contract_ids(progress_facts)
    )
    selected_expected_workflow = (
        not case.expected_workflow_id
        or case.expected_workflow_id in set(selected_workflow_ids)
    )
    route_evidence_present = _selector_route_evidence_present(
        selected_workflow_ids,
        observed_workflow_ids,
        selector_diagnostics,
    )
    last_task_status = _as_mapping(task_evidence.get("last_task_status"))
    terminal_status = _safe_text(last_task_status.get("status")) or "unknown"
    workflow_preflight = dict(
        workflow_capability_preflight
        if isinstance(workflow_capability_preflight, Mapping)
        else {"workflow_discovery_available": True}
    )
    precondition_blockers = build_precondition_blockers(
        auth_login=auth_login,
        auth_status=auth_status,
        target_session_context=target_session_context,
        database_preflight=database_preflight,
        llm_preflight=llm_preflight,
        workflow_capability_preflight=workflow_preflight,
        gmail_preflight=gmail_preflight,
        gmail_forced=not case.requires_gmail,
    )
    context_build_blocker = (
        _context_build_progress_blocker(
            terminal_status=terminal_status,
            last_task_status=last_task_status,
            selector_diagnostics=selector_diagnostics,
        )
        if terminal_status != "completed" and not route_evidence_present
        else None
    )

    blocker: dict[str, Any] | None = None
    if precondition_blockers:
        blocker = dict(precondition_blockers[0])
    elif context_build_blocker is not None:
        blocker = context_build_blocker
    elif not selected_expected_workflow and (
        route_evidence_present or terminal_status == "completed"
    ):
        blocker = {
            "type": "selector_or_dispatch_blocker",
            "reason": (
                f"Expected {case.expected_workflow_id} but observed "
                f"selected/dispatch workflow IDs {list(selected_workflow_ids)!r} "
                f"and execution workflow IDs {list(observed_workflow_ids)!r}."
            ),
            "selected_workflow_ids": list(selected_workflow_ids),
            "observed_workflow_ids": list(observed_workflow_ids),
            "selector_diagnostics": [dict(item) for item in selector_diagnostics],
        }
    elif (
        terminal_status not in {"completed"}
        and selected_expected_workflow
        and route_evidence_present
    ):
        blocker = _terminal_workflow_action_blocker(
            terminal_status=terminal_status,
            last_task_status=last_task_status,
            selected_workflow_ids=selected_workflow_ids,
            observed_workflow_ids=observed_workflow_ids,
            missing_fact_ids=missing_fact_ids,
            missing_contract_ids=missing_contract_ids,
        )
    elif terminal_status not in {"completed"}:
        blocker = {
            "type": "task_terminal_state_blocker",
            "reason": f"Background task ended with status {terminal_status!r}.",
            "last_task_status": dict(last_task_status),
        }
    elif not selected_expected_workflow:
        blocker = {
            "type": "selector_or_dispatch_blocker",
            "reason": (
                f"Expected {case.expected_workflow_id} but no selected, "
                "dispatch, or execution workflow evidence was captured."
            ),
            "selected_workflow_ids": list(selected_workflow_ids),
            "observed_workflow_ids": list(observed_workflow_ids),
            "selector_diagnostics": [dict(item) for item in selector_diagnostics],
        }
    elif missing_fact_ids or missing_contract_ids:
        blocker = {
            "type": "thinking_card_projection_blocker",
            "reason": "Replay reached the expected workflow, but progress projection evidence is incomplete.",
            "missing_fact_ids": missing_fact_ids,
            "missing_contract_ids": missing_contract_ids,
        }
    elif _contains_tool_registration_blocker_text(task_evidence):
        blocker = {
            "type": "tool_registration_blocker",
            "reason": "Task evidence contains an unregistered tool or MCP method failure.",
            "observed_tools": extract_observed_tool_names(task_evidence),
        }
    elif _contains_gmail_oauth_blocker_text(task_evidence):
        blocker = {
            "type": "gmail_oauth_or_profile_blocker",
            "reason": "Task evidence contains Gmail OAuth/profile failure text.",
        }

    return {
        "verdict": "pass" if blocker is None else "blocked",
        "blocker": blocker,
        "selected_expected_workflow": selected_expected_workflow,
        "selected_workflow_ids": list(selected_workflow_ids),
        "observed_workflow_ids": list(observed_workflow_ids),
        "selector_diagnostics": [dict(item) for item in selector_diagnostics],
        "target_session_context": dict(target_session_context or {}),
        "database_preflight": dict(database_preflight or {}),
        "llm_preflight": dict(llm_preflight or {}),
        "workflow_capability_preflight": workflow_preflight,
        "precondition_blockers": [dict(item) for item in precondition_blockers],
        "observed_progress_fact_ids": sorted(_fact_ids(progress_facts)),
        "observed_contract_ids": sorted(_contract_ids(progress_facts)),
        "missing_progress_fact_ids": missing_fact_ids,
        "missing_contract_ids": missing_contract_ids,
        "terminal_task_status": terminal_status,
    }


def build_replay_report(
    *,
    case: ReplayCase,
    environment: Mapping[str, Any],
    window_session_id: str,
    auth_login: Mapping[str, Any],
    auth_status: Mapping[str, Any],
    target_session_context: Mapping[str, Any],
    database_preflight: Mapping[str, Any],
    llm_preflight: Mapping[str, Any],
    gmail_preflight: Mapping[str, Any],
    chat_session: Mapping[str, Any],
    submission: Mapping[str, Any],
    task_evidence: Mapping[str, Any],
    workflow_capability_preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    task_result = _as_mapping(task_evidence.get("task_result"))
    task_statuses = _as_list(task_evidence.get("task_statuses"))
    progress_snapshots = _as_list(task_evidence.get("progress_snapshots"))
    evidence_payloads = (
        task_statuses,
        progress_snapshots,
        task_result,
        task_evidence.get("last_progress"),
        task_evidence.get("last_task_status"),
    )
    selected_workflow_ids = extract_selected_workflow_ids(
        *evidence_payloads,
    )
    observed_workflow_ids = extract_observed_workflow_ids(
        *evidence_payloads,
    )
    selector_diagnostics = extract_selector_diagnostics(
        *evidence_payloads,
    )
    progress_facts = extract_progress_facts(
        *evidence_payloads,
    )
    analysis = classify_replay(
        case=case,
        auth_login=auth_login,
        auth_status=auth_status,
        target_session_context=target_session_context,
        database_preflight=database_preflight,
        llm_preflight=llm_preflight,
        gmail_preflight=gmail_preflight,
        task_evidence=task_evidence,
        selected_workflow_ids=selected_workflow_ids,
        observed_workflow_ids=observed_workflow_ids,
        selector_diagnostics=selector_diagnostics,
        progress_facts=progress_facts,
        workflow_capability_preflight=workflow_capability_preflight,
    )
    workflow_preflight = dict(
        workflow_capability_preflight
        if isinstance(workflow_capability_preflight, Mapping)
        else {"workflow_discovery_available": True}
    )
    return {
        "schema_version": "authenticated_browser_workflow_replay.v1",
        "case": {
            "case_id": case.case_id,
            "prompt": case.prompt,
            "expected_workflow_id": case.expected_workflow_id,
            "expected_progress_fact_ids": list(case.expected_progress_fact_ids),
            "expected_contract_ids": list(case.expected_contract_ids),
        },
        "environment": dict(environment),
        "window_session_id": window_session_id,
        "auth_login": dict(auth_login),
        "auth_status": dict(auth_status),
        "target_session_context": dict(target_session_context),
        "database_preflight": dict(database_preflight),
        "llm_preflight": dict(llm_preflight),
        "workflow_capability_preflight": workflow_preflight,
        "gmail_preflight": dict(gmail_preflight),
        "chat_session": dict(chat_session),
        "submission": dict(submission),
        "task_id": _safe_text(submission.get("task_id")) or None,
        "request_id": _safe_text(submission.get("request_id"))
        or _safe_text(submission.get("task_id"))
        or None,
        "visible_answer": extract_visible_answer(task_result),
        "thinking_card_progress_facts": progress_facts,
        "selector_diagnostics": selector_diagnostics,
        "task_status_snapshot_count": len(task_statuses),
        "progress_snapshot_count": len(progress_snapshots),
        "task_statuses": task_statuses,
        "progress_snapshots": progress_snapshots,
        "last_progress": _as_mapping(task_evidence.get("last_progress")),
        "last_task_status": _as_mapping(task_evidence.get("last_task_status")),
        "task_result": task_result,
        "analysis": analysis,
    }


def run_replay(
    *,
    case: ReplayCase,
    base_url: str,
    gmail_profile: str | None,
    model: str | None,
    target_user_concept_id: str | None,
    target_organisation_concept_id: str | None,
    presenter_mode: bool,
    thinking_card_mode: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    auth_login_timeout_seconds: float,
    database_preflight_attempts: int,
    database_preflight_poll_interval_seconds: float,
    workflow_capability_preflight_timeout_seconds: float,
    workflow_capability_preflight_poll_interval_seconds: float,
    allow_non_agent_test_server: bool,
    run_despite_database_preflight_blocker: bool,
    run_despite_llm_preflight_blocker: bool,
    run_despite_workflow_capability_preflight_blocker: bool,
    run_despite_gmail_preflight_blocker: bool,
    cancel_on_timeout: bool,
) -> dict[str, Any]:
    session = requests.Session()
    environment = collect_run_environment(session=session, base_url=base_url)
    require_agent_test_server(
        environment=environment,
        base_url=base_url,
        allow_non_agent_test_server=allow_non_agent_test_server,
    )

    window_session_id = f"browser-replay-{uuid.uuid4()}"
    auth_login = establish_browser_test_session(
        session=session,
        base_url=base_url,
        window_session_id=window_session_id,
        timeout_seconds=auth_login_timeout_seconds,
    )
    user_concept_id = _safe_text(auth_login.get("user_concept_id"))
    if user_concept_id:
        session.headers.update({"X-User-Concept-ID": user_concept_id})
    effective_target_user = _safe_text(target_user_concept_id) or user_concept_id
    target_session_context = apply_target_session_context(
        session=session,
        base_url=base_url,
        user_concept_id=effective_target_user,
        organisation_concept_id=target_organisation_concept_id,
    )
    auth_status = get_auth_status(session=session, base_url=base_url)
    database_preflight = build_database_preflight(
        session=session,
        base_url=base_url,
        attempt_count=database_preflight_attempts,
        poll_interval_seconds=database_preflight_poll_interval_seconds,
    )
    llm_preflight = build_llm_preflight(
        session=session,
        base_url=base_url,
        user_concept_id=effective_target_user,
        organisation_concept_id=target_organisation_concept_id,
        explicit_model=model,
    )
    workflow_capability_preflight = build_workflow_capability_preflight(
        session=session,
        base_url=base_url,
        timeout_seconds=workflow_capability_preflight_timeout_seconds,
        poll_interval_seconds=workflow_capability_preflight_poll_interval_seconds,
    )
    gmail_preflight = build_gmail_preflight(
        session=session,
        base_url=base_url,
        gmail_profile=gmail_profile,
        required=case.requires_gmail,
    )
    chat_session = {}
    submission = {}
    task_evidence = {
        "task_statuses": [],
        "progress_snapshots": [],
        "last_task_status": {},
        "last_progress": {},
        "task_result": {},
    }
    gmail_ready_or_forced = bool(
        gmail_preflight.get("gmail_capability_ready")
        or run_despite_gmail_preflight_blocker
    )
    workflow_capability_ready_or_forced = bool(
        workflow_capability_preflight.get("workflow_discovery_available")
        or run_despite_workflow_capability_preflight_blocker
    )
    database_ready_or_forced = bool(
        database_preflight.get("database_runtime_available")
        or run_despite_database_preflight_blocker
    )
    llm_ready_or_forced = bool(
        llm_preflight.get("llm_runtime_available") or run_despite_llm_preflight_blocker
    )
    target_context_ready = bool(
        target_session_context.get("target_session_context_ready") is not False
    )
    auth_ready = bool(
        auth_login.get("success")
        or auth_status.get("authenticated")
        or target_context_ready
    )
    active_precondition_blockers = build_precondition_blockers(
        auth_login=auth_login,
        auth_status=auth_status,
        target_session_context=target_session_context,
        database_preflight=database_preflight,
        llm_preflight=llm_preflight,
        workflow_capability_preflight=workflow_capability_preflight,
        gmail_preflight=gmail_preflight,
        database_forced=run_despite_database_preflight_blocker,
        llm_forced=run_despite_llm_preflight_blocker,
        workflow_capability_forced=run_despite_workflow_capability_preflight_blocker,
        gmail_forced=run_despite_gmail_preflight_blocker,
    )
    if active_precondition_blockers:
        submission = {
            "skipped": True,
            "skip_reason": "preflight_blocker",
            "precondition_blockers": active_precondition_blockers,
        }
    elif (
        auth_ready
        and target_context_ready
        and database_ready_or_forced
        and llm_ready_or_forced
        and workflow_capability_ready_or_forced
        and gmail_ready_or_forced
    ):
        chat_session = create_replay_chat_session(
            session=session,
            base_url=base_url,
            case_id=case.case_id,
        )
        conversation_session_id = _safe_text(chat_session.get("session_id"))
        client_request_id = f"jvnautosci-2422-{uuid.uuid4()}"
        submission = submit_background_generate(
            session=session,
            base_url=base_url,
            case=case,
            client_request_id=client_request_id,
            conversation_session_id=conversation_session_id,
            gmail_profile=_safe_text(gmail_preflight.get("requested_gmail_profile")),
            model=model,
            presenter_mode=presenter_mode,
            thinking_card_mode=thinking_card_mode,
        )
        task_id = _safe_text(submission.get("task_id"))
        request_id = _safe_text(submission.get("request_id")) or task_id
        if task_id and request_id:
            task_evidence = poll_replay_task(
                session=session,
                base_url=base_url,
                task_id=task_id,
                request_id=request_id,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                cancel_on_timeout=cancel_on_timeout,
            )

    return build_replay_report(
        case=case,
        environment=environment,
        window_session_id=window_session_id,
        auth_login=auth_login,
        auth_status=auth_status,
        target_session_context=target_session_context,
        database_preflight=database_preflight,
        llm_preflight=llm_preflight,
        workflow_capability_preflight=workflow_capability_preflight,
        gmail_preflight=gmail_preflight,
        chat_session=chat_session,
        submission=submission,
        task_evidence=task_evidence,
    )


def build_gmail_arxiv_idempotence_turn_cases(
    *,
    gmail_query: str | None = None,
) -> tuple[ReplayTurnCase, ...]:
    query = _safe_text(gmail_query) or GMAIL_ARXIV_IDEMPOTENCE_DEFAULT_QUERY
    turns: list[ReplayTurnCase] = []
    for turn_id, prompt_template in GMAIL_ARXIV_IDEMPOTENCE_TURN_PROMPTS:
        prompt = prompt_template.format(
            gmail_query=query,
            processing_marker=GMAIL_ARXIV_IDEMPOTENCE_PROCESSING_MARKER,
        )
        turns.append(
            ReplayTurnCase(
                turn_id=turn_id,
                case=ReplayCase(
                    case_id=f"{GMAIL_ARXIV_IDEMPOTENCE_REPLAY_CASE_ID}_{turn_id}",
                    prompt=prompt,
                    expected_workflow_id=(
                        GMAIL_ARXIV_WORKFLOW_ID if turn_id == "initial" else None
                    ),
                    expected_progress_fact_ids=(),
                    expected_contract_ids=(),
                    requires_gmail=True,
                    workflow_inputs=(
                        {
                            "base_gmail_query": query,
                            "gmail_max_results": 1,
                            "max_results": 1,
                        }
                        if turn_id == "initial"
                        else None
                    ),
                ),
            )
        )
    return tuple(turns)


def run_gmail_arxiv_idempotence_replay(
    *,
    base_url: str,
    gmail_profile: str | None,
    gmail_query: str | None,
    model: str | None,
    target_user_concept_id: str | None,
    target_organisation_concept_id: str | None,
    presenter_mode: bool,
    thinking_card_mode: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    auth_login_timeout_seconds: float,
    database_preflight_attempts: int,
    database_preflight_poll_interval_seconds: float,
    workflow_capability_preflight_timeout_seconds: float,
    workflow_capability_preflight_poll_interval_seconds: float,
    allow_non_agent_test_server: bool,
    run_despite_database_preflight_blocker: bool,
    run_despite_llm_preflight_blocker: bool,
    run_despite_workflow_capability_preflight_blocker: bool,
    run_despite_gmail_preflight_blocker: bool,
    cancel_on_timeout: bool,
) -> dict[str, Any]:
    """Run the 2571 first/repeat/verify sequence in one chat session."""

    turn_cases = build_gmail_arxiv_idempotence_turn_cases(gmail_query=gmail_query)
    session = requests.Session()
    environment = collect_run_environment(session=session, base_url=base_url)
    require_agent_test_server(
        environment=environment,
        base_url=base_url,
        allow_non_agent_test_server=allow_non_agent_test_server,
    )

    window_session_id = f"browser-replay-{uuid.uuid4()}"
    auth_login = establish_browser_test_session(
        session=session,
        base_url=base_url,
        window_session_id=window_session_id,
        timeout_seconds=auth_login_timeout_seconds,
    )
    user_concept_id = _safe_text(auth_login.get("user_concept_id"))
    if user_concept_id:
        session.headers.update({"X-User-Concept-ID": user_concept_id})
    effective_target_user = _safe_text(target_user_concept_id) or user_concept_id
    target_session_context = apply_target_session_context(
        session=session,
        base_url=base_url,
        user_concept_id=effective_target_user,
        organisation_concept_id=target_organisation_concept_id,
    )
    auth_status = get_auth_status(session=session, base_url=base_url)
    database_preflight = build_database_preflight(
        session=session,
        base_url=base_url,
        attempt_count=database_preflight_attempts,
        poll_interval_seconds=database_preflight_poll_interval_seconds,
    )
    llm_preflight = build_llm_preflight(
        session=session,
        base_url=base_url,
        user_concept_id=effective_target_user,
        organisation_concept_id=target_organisation_concept_id,
        explicit_model=model,
    )
    workflow_capability_preflight = build_workflow_capability_preflight(
        session=session,
        base_url=base_url,
        timeout_seconds=workflow_capability_preflight_timeout_seconds,
        poll_interval_seconds=workflow_capability_preflight_poll_interval_seconds,
    )
    gmail_preflight = build_gmail_preflight(
        session=session,
        base_url=base_url,
        gmail_profile=gmail_profile,
        required=True,
    )

    active_precondition_blockers = build_precondition_blockers(
        auth_login=auth_login,
        auth_status=auth_status,
        target_session_context=target_session_context,
        database_preflight=database_preflight,
        llm_preflight=llm_preflight,
        workflow_capability_preflight=workflow_capability_preflight,
        gmail_preflight=gmail_preflight,
        database_forced=run_despite_database_preflight_blocker,
        llm_forced=run_despite_llm_preflight_blocker,
        workflow_capability_forced=run_despite_workflow_capability_preflight_blocker,
        gmail_forced=run_despite_gmail_preflight_blocker,
    )

    chat_session: dict[str, Any] = {}
    turn_reports: list[dict[str, Any]] = []
    if active_precondition_blockers:
        sequence_analysis = _blocked_sequence_analysis(
            _safe_text(active_precondition_blockers[0].get("type"))
            or "preflight_blocker",
            _safe_text(active_precondition_blockers[0].get("reason"))
            or "Replay preflight blocked submission.",
            evidence={"precondition_blockers": active_precondition_blockers},
            turn_reports=turn_reports,
        )
    else:
        chat_session = create_replay_chat_session(
            session=session,
            base_url=base_url,
            case_id=GMAIL_ARXIV_IDEMPOTENCE_REPLAY_CASE_ID,
        )
        conversation_session_id = _safe_text(chat_session.get("session_id"))
        for turn in turn_cases:
            client_request_id = f"jvnautosci-2571-{turn.turn_id}-{uuid.uuid4()}"
            submission = submit_background_generate(
                session=session,
                base_url=base_url,
                case=turn.case,
                client_request_id=client_request_id,
                conversation_session_id=conversation_session_id,
                gmail_profile=_safe_text(
                    gmail_preflight.get("requested_gmail_profile")
                ),
                model=model,
                presenter_mode=presenter_mode,
                thinking_card_mode=thinking_card_mode,
            )
            task_evidence = {
                "task_statuses": [],
                "progress_snapshots": [],
                "last_task_status": {},
                "last_progress": {},
                "task_result": {},
            }
            task_id = _safe_text(submission.get("task_id"))
            request_id = _safe_text(submission.get("request_id")) or task_id
            if task_id and request_id:
                task_evidence = poll_replay_task(
                    session=session,
                    base_url=base_url,
                    task_id=task_id,
                    request_id=request_id,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    cancel_on_timeout=cancel_on_timeout,
                )
            report = build_replay_report(
                case=turn.case,
                environment=environment,
                window_session_id=window_session_id,
                auth_login=auth_login,
                auth_status=auth_status,
                target_session_context=target_session_context,
                database_preflight=database_preflight,
                llm_preflight=llm_preflight,
                workflow_capability_preflight=workflow_capability_preflight,
                gmail_preflight=gmail_preflight,
                chat_session=chat_session,
                submission=submission,
                task_evidence=task_evidence,
            )
            report["turn_id"] = turn.turn_id
            turn_reports.append(report)

            if _safe_text(_as_mapping(report.get("analysis")).get("verdict")) == (
                "blocked"
            ):
                break

        sequence_analysis = analyse_gmail_arxiv_idempotence_sequence(turn_reports)

    return {
        "schema_version": "authenticated_browser_workflow_replay_sequence.v1",
        "case_id": GMAIL_ARXIV_IDEMPOTENCE_REPLAY_CASE_ID,
        "case": {
            "case_id": GMAIL_ARXIV_IDEMPOTENCE_REPLAY_CASE_ID,
            "gmail_query": (
                _safe_text(gmail_query) or GMAIL_ARXIV_IDEMPOTENCE_DEFAULT_QUERY
            ),
            "processing_marker": GMAIL_ARXIV_IDEMPOTENCE_PROCESSING_MARKER,
            "turn_ids": [turn.turn_id for turn in turn_cases],
        },
        "environment": dict(environment),
        "window_session_id": window_session_id,
        "auth_login": dict(auth_login),
        "auth_status": dict(auth_status),
        "target_session_context": dict(target_session_context),
        "database_preflight": dict(database_preflight),
        "llm_preflight": dict(llm_preflight),
        "workflow_capability_preflight": dict(workflow_capability_preflight),
        "gmail_preflight": dict(gmail_preflight),
        "precondition_blockers": active_precondition_blockers,
        "chat_session": dict(chat_session),
        "turn_reports": turn_reports,
        "analysis": sequence_analysis,
    }


def _write_output(path: str | None, report: Mapping[str, Any]) -> None:
    output = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not path:
        print(output, end="")
        return
    output_path = Path(path)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")


def _resolve_case(
    case_id: str,
    *,
    prompt: str | None = None,
    expected_workflow_id: str | None = None,
    expected_progress_fact_ids: Sequence[str] | None = None,
    expected_contract_ids: Sequence[str] | None = None,
    requires_gmail: bool = False,
) -> ReplayCase:
    prompt_text = _safe_text(prompt)
    if prompt_text:
        return ReplayCase(
            case_id=_safe_text(case_id) or f"ad_hoc_{uuid.uuid4()}",
            prompt=prompt_text,
            expected_workflow_id=_safe_text(expected_workflow_id) or None,
            expected_progress_fact_ids=tuple(
                item
                for item in (
                    _safe_text(value) for value in (expected_progress_fact_ids or ())
                )
                if item
            ),
            expected_contract_ids=tuple(
                item
                for item in (
                    _safe_text(value) for value in (expected_contract_ids or ())
                )
                if item
            ),
            requires_gmail=requires_gmail,
        )
    cleaned = _safe_text(case_id)
    if cleaned in {"gmail-arxiv-2421", GMAIL_ARXIV_REPLAY_CASE.case_id}:
        return GMAIL_ARXIV_REPLAY_CASE
    raise RuntimeError(f"Unknown replay case: {case_id}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run an authenticated browser-test replay through Von's real "
            "/von/generate and Thinking-card progress path."
        )
    )
    parser.add_argument("--case", default="gmail-arxiv-2421")
    parser.add_argument(
        "--idempotence-gmail-query",
        default=GMAIL_ARXIV_IDEMPOTENCE_DEFAULT_QUERY,
        help=(
            "Gmail query described to the 2571 idempotence replay prompt when "
            "--case gmail-arxiv-idempotence-2571 is selected."
        ),
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help=(
            "Run an ad-hoc replay prompt instead of a named replay case. "
            "Workflow expectations remain optional telemetry assertions."
        ),
    )
    parser.add_argument(
        "--expected-workflow-id",
        default=None,
        help="Expected selected workflow ID for ad-hoc replay analysis.",
    )
    parser.add_argument(
        "--expected-progress-fact-id",
        action="append",
        default=[],
        help="Expected progress fact ID; may be supplied multiple times.",
    )
    parser.add_argument(
        "--expected-contract-id",
        action="append",
        default=[],
        help="Expected progress contract ID; may be supplied multiple times.",
    )
    parser.add_argument(
        "--requires-gmail",
        action="store_true",
        help="Require Gmail profile/OAuth preflight for an ad-hoc prompt.",
    )
    parser.add_argument("--base-url", default=get_default_agent_test_base_url())
    parser.add_argument("--gmail-profile", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--target-user-concept-id", default=None)
    parser.add_argument(
        "--target-organisation-concept-id",
        "--target-organization-concept-id",
        dest="target_organisation_concept_id",
        default=None,
    )
    parser.add_argument("--thinking-card-mode", default="debug")
    parser.add_argument("--presenter-mode", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--auth-login-timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--database-preflight-attempts",
        type=int,
        default=3,
        help=(
            "Number of Mongo read/write health samples required before replay "
            "submission."
        ),
    )
    parser.add_argument(
        "--database-preflight-poll-interval-seconds",
        type=float,
        default=0.75,
        help="Delay between Mongo read/write preflight samples.",
    )
    parser.add_argument(
        "--workflow-capability-preflight-timeout-seconds",
        type=float,
        default=90.0,
        help=(
            "Maximum time to wait for represented workflow discovery to become "
            "available before classifying the replay as blocked."
        ),
    )
    parser.add_argument(
        "--workflow-capability-preflight-poll-interval-seconds",
        type=float,
        default=1.5,
        help="Polling interval for the workflow capability preflight wait.",
    )
    parser.add_argument("--output-json", default=None)
    parser.add_argument(
        "--skip-cancel-on-timeout",
        action="store_true",
        help=(
            "Write the evidence report at timeout without calling the task "
            "cancel endpoint. Useful when diagnosing cancellation/read-back hangs."
        ),
    )
    parser.add_argument(
        "--run-despite-gmail-preflight-blocker",
        action="store_true",
        help=(
            "Submit /von/generate even when the Gmail profile/OAuth preflight "
            "already indicates that the replay is blocked."
        ),
    )
    parser.add_argument(
        "--run-despite-database-preflight-blocker",
        action="store_true",
        help=(
            "Submit /von/generate even when Mongo read/write preflight is "
            "unhealthy. Use only for diagnostic run-throughs."
        ),
    )
    parser.add_argument(
        "--run-despite-llm-preflight-blocker",
        action="store_true",
        help=(
            "Submit /von/generate even when the target user's effective model "
            "is unavailable. Use only for diagnostic run-throughs."
        ),
    )
    parser.add_argument(
        "--run-despite-workflow-capability-preflight-blocker",
        action="store_true",
        help=(
            "Submit /von/generate even when the workflow capability index "
            "preflight says represented workflow discovery is unavailable. "
            "Use only for diagnostic run-throughs, not acceptance evidence."
        ),
    )
    parser.add_argument(
        "--allow-non-agent-test-server",
        action="store_true",
        help=(
            "Permit replay against a server whose /health payload does not "
            "report agent_test_instance=true."
        ),
    )
    args = parser.parse_args(argv)

    if _safe_text(args.case) in {
        GMAIL_ARXIV_IDEMPOTENCE_CASE_ID,
        GMAIL_ARXIV_IDEMPOTENCE_REPLAY_CASE_ID,
    } and not _safe_text(args.prompt):
        report = run_gmail_arxiv_idempotence_replay(
            base_url=str(args.base_url).rstrip("/"),
            gmail_profile=args.gmail_profile,
            gmail_query=args.idempotence_gmail_query,
            model=args.model,
            target_user_concept_id=args.target_user_concept_id,
            target_organisation_concept_id=args.target_organisation_concept_id,
            presenter_mode=bool(args.presenter_mode),
            thinking_card_mode=args.thinking_card_mode,
            timeout_seconds=float(args.timeout_seconds),
            poll_interval_seconds=float(args.poll_interval_seconds),
            auth_login_timeout_seconds=float(args.auth_login_timeout_seconds),
            database_preflight_attempts=int(args.database_preflight_attempts),
            database_preflight_poll_interval_seconds=float(
                args.database_preflight_poll_interval_seconds
            ),
            workflow_capability_preflight_timeout_seconds=float(
                args.workflow_capability_preflight_timeout_seconds
            ),
            workflow_capability_preflight_poll_interval_seconds=float(
                args.workflow_capability_preflight_poll_interval_seconds
            ),
            allow_non_agent_test_server=bool(args.allow_non_agent_test_server),
            run_despite_database_preflight_blocker=bool(
                args.run_despite_database_preflight_blocker
            ),
            run_despite_llm_preflight_blocker=bool(
                args.run_despite_llm_preflight_blocker
            ),
            run_despite_workflow_capability_preflight_blocker=bool(
                args.run_despite_workflow_capability_preflight_blocker
            ),
            run_despite_gmail_preflight_blocker=bool(
                args.run_despite_gmail_preflight_blocker
            ),
            cancel_on_timeout=not bool(args.skip_cancel_on_timeout),
        )
        _write_output(args.output_json, report)
        verdict = _safe_text(_as_mapping(report.get("analysis")).get("verdict"))
        return 0 if verdict in {"pass", "blocked"} else 1

    case = _resolve_case(
        args.case,
        prompt=args.prompt,
        expected_workflow_id=args.expected_workflow_id,
        expected_progress_fact_ids=args.expected_progress_fact_id,
        expected_contract_ids=args.expected_contract_id,
        requires_gmail=bool(args.requires_gmail),
    )
    report = run_replay(
        case=case,
        base_url=str(args.base_url).rstrip("/"),
        gmail_profile=args.gmail_profile,
        model=args.model,
        target_user_concept_id=args.target_user_concept_id,
        target_organisation_concept_id=args.target_organisation_concept_id,
        presenter_mode=bool(args.presenter_mode),
        thinking_card_mode=args.thinking_card_mode,
        timeout_seconds=float(args.timeout_seconds),
        poll_interval_seconds=float(args.poll_interval_seconds),
        auth_login_timeout_seconds=float(args.auth_login_timeout_seconds),
        database_preflight_attempts=int(args.database_preflight_attempts),
        database_preflight_poll_interval_seconds=float(
            args.database_preflight_poll_interval_seconds
        ),
        workflow_capability_preflight_timeout_seconds=float(
            args.workflow_capability_preflight_timeout_seconds
        ),
        workflow_capability_preflight_poll_interval_seconds=float(
            args.workflow_capability_preflight_poll_interval_seconds
        ),
        allow_non_agent_test_server=bool(args.allow_non_agent_test_server),
        run_despite_database_preflight_blocker=bool(
            args.run_despite_database_preflight_blocker
        ),
        run_despite_llm_preflight_blocker=bool(args.run_despite_llm_preflight_blocker),
        run_despite_workflow_capability_preflight_blocker=bool(
            args.run_despite_workflow_capability_preflight_blocker
        ),
        run_despite_gmail_preflight_blocker=bool(
            args.run_despite_gmail_preflight_blocker
        ),
        cancel_on_timeout=not bool(args.skip_cancel_on_timeout),
    )
    _write_output(args.output_json, report)
    verdict = _safe_text(_as_mapping(report.get("analysis")).get("verdict"))
    return 0 if verdict in {"pass", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
