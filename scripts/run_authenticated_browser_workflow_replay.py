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


GMAIL_ARXIV_REPLAY_CASE = ReplayCase(
    case_id="gmail_arxiv_ingestion_2421_motivating_prompt",
    prompt=GMAIL_ARXIV_PROMPT,
    expected_workflow_id=GMAIL_ARXIV_WORKFLOW_ID,
    expected_progress_fact_ids=GMAIL_ARXIV_FACT_IDS,
    expected_contract_ids=GMAIL_ARXIV_CONTRACT_IDS,
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
    return {
        "version": _safe_text(payload.get("version")) or None,
        "git_branch": _safe_text(version_details.get("git_branch")) or None,
        "git_commit": _safe_text(version_details.get("git_commit")) or None,
        "git_dirty": version_details.get("git_dirty"),
        "agent_test_instance": payload.get("agent_test_instance"),
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
            "server_agent_test_instance": environment.get(
                "server_agent_test_instance"
            ),
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
            json={"window_session_id": window_session_id},
        )
    except Exception as exc:
        payload = exc.payload if isinstance(exc, JsonRequestError) else {}
        return {
            "success": False,
            "error": str(exc),
            "status_code": exc.status_code if isinstance(exc, JsonRequestError) else None,
            "payload": payload,
            "browser_test_mode": _as_mapping(payload.get("browser_test_mode")),
        }


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


def build_gmail_preflight(
    *,
    session: requests.Session,
    base_url: str,
    gmail_profile: str | None,
) -> dict[str, Any]:
    settings = _query_settings(session=session, base_url=base_url)
    profiles = _as_list(settings.get("gmail_profiles"))
    default_profile = _safe_text(settings.get("gmail_default_profile")) or None
    requested_profile = _safe_text(gmail_profile) or default_profile
    oauth_status = _query_gmail_oauth_status(
        session=session,
        base_url=base_url,
        gmail_profile=requested_profile,
    )
    return {
        "requested_gmail_profile": requested_profile,
        "configured_gmail_profiles": profiles,
        "default_gmail_profile": default_profile,
        "gmail_profiles_configured": bool(profiles),
        "requested_profile_configured": (
            bool(requested_profile) and requested_profile in profiles
        ),
        "oauth_status": oauth_status,
        "gmail_capability_ready": bool(
            requested_profile
            and requested_profile in profiles
            and isinstance(oauth_status, Mapping)
            and oauth_status.get("has_tokens") is True
        ),
    }


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
            if workflow_id and workflow_id not in workflow_ids:
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
                            nested_entry[nested_key] = [
                                item for item in items if item
                            ][:20]
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


def _contains_gmail_oauth_blocker_text(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True).lower()
    gmailish = any(marker in text for marker in ("gmail", "google mail"))
    authish = any(
        marker in text
        for marker in (
            "oauth",
            "token",
            "credential",
            "authoris",
            "authoriz",
            "profile",
            "not configured",
            "not authenticated",
            "not connected",
        )
    )
    return gmailish and authish


def classify_replay(
    *,
    case: ReplayCase,
    auth_login: Mapping[str, Any],
    auth_status: Mapping[str, Any],
    gmail_preflight: Mapping[str, Any],
    task_evidence: Mapping[str, Any],
    selected_workflow_ids: Sequence[str],
    observed_workflow_ids: Sequence[str],
    selector_diagnostics: Sequence[Mapping[str, Any]],
    progress_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    missing_fact_ids = sorted(set(case.expected_progress_fact_ids) - _fact_ids(progress_facts))
    missing_contract_ids = sorted(set(case.expected_contract_ids) - _contract_ids(progress_facts))
    selected_expected_workflow = (
        not case.expected_workflow_id
        or case.expected_workflow_id in set(selected_workflow_ids)
    )
    route_evidence_present = bool(
        selected_workflow_ids or observed_workflow_ids or selector_diagnostics
    )
    last_task_status = _as_mapping(task_evidence.get("last_task_status"))
    terminal_status = _safe_text(last_task_status.get("status")) or "unknown"

    blocker: dict[str, Any] | None = None
    if not auth_login.get("success") or not auth_status.get("authenticated"):
        blocker = {
            "type": "browser_test_auth_blocker",
            "reason": _safe_text(auth_login.get("error"))
            or _safe_text(auth_status.get("error"))
            or "Browser-test login did not establish an authenticated session.",
        }
    elif not gmail_preflight.get("gmail_capability_ready"):
        blocker = {
            "type": "gmail_oauth_or_profile_blocker",
            "reason": "Gmail profile/tokens are not ready for an authenticated Gmail-backed replay.",
            "gmail_preflight": dict(gmail_preflight),
        }
    elif (
        not selected_expected_workflow
        and (route_evidence_present or terminal_status == "completed")
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
    gmail_preflight: Mapping[str, Any],
    chat_session: Mapping[str, Any],
    submission: Mapping[str, Any],
    task_evidence: Mapping[str, Any],
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
        gmail_preflight=gmail_preflight,
        task_evidence=task_evidence,
        selected_workflow_ids=selected_workflow_ids,
        observed_workflow_ids=observed_workflow_ids,
        selector_diagnostics=selector_diagnostics,
        progress_facts=progress_facts,
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
    presenter_mode: bool,
    thinking_card_mode: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    auth_login_timeout_seconds: float,
    allow_non_agent_test_server: bool,
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
    auth_status = get_auth_status(session=session, base_url=base_url)
    gmail_preflight = build_gmail_preflight(
        session=session,
        base_url=base_url,
        gmail_profile=gmail_profile,
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
    if (
        auth_login.get("success")
        and auth_status.get("authenticated")
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
        gmail_preflight=gmail_preflight,
        chat_session=chat_session,
        submission=submission,
        task_evidence=task_evidence,
    )


def _write_output(path: str | None, report: Mapping[str, Any]) -> None:
    output = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not path:
        print(output, end="")
        return
    output_path = Path(path)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")


def _resolve_case(case_id: str) -> ReplayCase:
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
    parser.add_argument("--base-url", default=get_default_agent_test_base_url())
    parser.add_argument("--gmail-profile", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--thinking-card-mode", default="debug")
    parser.add_argument("--presenter-mode", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--auth-login-timeout-seconds", type=float, default=180.0)
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
        "--allow-non-agent-test-server",
        action="store_true",
        help=(
            "Permit replay against a server whose /health payload does not "
            "report agent_test_instance=true."
        ),
    )
    args = parser.parse_args(argv)

    case = _resolve_case(args.case)
    report = run_replay(
        case=case,
        base_url=str(args.base_url).rstrip("/"),
        gmail_profile=args.gmail_profile,
        model=args.model,
        presenter_mode=bool(args.presenter_mode),
        thinking_card_mode=args.thinking_card_mode,
        timeout_seconds=float(args.timeout_seconds),
        poll_interval_seconds=float(args.poll_interval_seconds),
        auth_login_timeout_seconds=float(args.auth_login_timeout_seconds),
        allow_non_agent_test_server=bool(args.allow_non_agent_test_server),
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
