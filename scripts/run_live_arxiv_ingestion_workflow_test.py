"""Run the canonical arXiv ingestion testing workflow against a live Von server.

By default this script uses the durable workflow API, which is the authoritative
programmatic surface for this acceptance test. An optional `ui` mode exercises
the `/von/generate` route using its background-task contract so long-running
workflow executions can still be validated through the UI surface.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
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

ARXIV_INGESTION_TESTING_WORKFLOW_ID = "#V#arxiv_paper_ingestion_testing_workflow"
ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#arxiv_paper_representation_workflow"
DEFAULT_ARXIV_SOURCE = "https://arxiv.org/abs/2603.21702"
DEFAULT_BASE_URL = DEFAULT_AGENT_TEST_BASE_URL
# UI mode routes validate the selected identity against the live server's test DB.
# Use a real person concept that survives header-authenticated identity validation
# on the test DB by default; callers can override this with --user-concept-id if
# they need a different authenticated UI user.
DEFAULT_UI_USER_CONCEPT_ID = "#V#person_hugues_van_assel_b0cd25a1"
DEFAULT_WORKFLOW_USER_ID = "#V#michael_witbrock"
DEFAULT_WORKFLOW_ORG_ID = "#V#default"
DEFAULT_WORKFLOW_NAMESPACE = "#V#michael_witbrock@default"
DEFAULT_MODEL = "gpt-5.4-nano"
EXPECTED_OBSERVATION_LABELS = {
    "target_workflow_execution",
    "metadata_representation",
    "author_links",
    "provenance_preserved",
    "cleanup_completed",
}
EXPECTED_METADATA_FLAGS = (
    "title_matched",
    "summary_matched",
    "publication_date_matched",
    "author_ids_matched",
    "author_names_matched",
    "topic_ids_matched",
    "arxiv_identifier_preserved",
    "source_uri_preserved",
    "file_copy_link_preserved",
)
TRANSIENT_WORKFLOW_ERROR_MARKERS = (
    "timed out",
    "server selection timeout",
    "networktimeout",
    "connection pool paused",
    "temporarily unavailable",
    "worker_exception",
)


def _require_agent_test_server(
    *,
    session: requests.Session,
    base_url: str,
    allow_non_agent_test_server: bool,
) -> None:
    if allow_non_agent_test_server:
        return
    try:
        health_payload = _request_json(
            session,
            "GET",
            f"{base_url}/health",
            timeout_seconds=15.0,
        )
    except Exception as exc:
        raise RuntimeError(
            "Live arXiv ingestion testing must use the isolated JVNAUTOSCI-2070 "
            "server setup. Start it with "
            r"`.\run.ps1 restart -AgentTest -HealthTimeoutSec 180` "
            f"and retry; health lookup failed for {base_url!r}: {exc}"
        ) from exc
    agent_test_error = build_agent_test_server_requirement_error(
        {
            "server_agent_test_instance": health_payload.get("agent_test_instance"),
            "server_metadata_source": "health",
            "server_metadata_error": None,
        },
        base_url=base_url,
    )
    if agent_test_error:
        raise RuntimeError(agent_test_error)


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


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    expected_status: int = 200,
    timeout_seconds: float = 120.0,
    **kwargs: Any,
) -> dict[str, Any]:
    resolved_method = str(method or "GET").upper()
    max_attempts = 6 if resolved_method == "GET" else 1
    attempt = 1
    while True:
        response = session.request(
            resolved_method,
            url,
            timeout=max(float(timeout_seconds), 1.0),
            **kwargs,
        )
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise RuntimeError(
                f"{resolved_method} {url} returned non-JSON payload (status={response.status_code}): "
                f"{response.text[:500]}"
            ) from exc
        if response.status_code == expected_status:
            return payload
        is_retryable_get = (
            resolved_method == "GET"
            and response.status_code == 503
            and bool(payload.get("retryable"))
            and attempt < max_attempts
        )
        if is_retryable_get:
            retry_after_raw = payload.get("retry_after_seconds")
            try:
                retry_after_seconds = max(float(retry_after_raw), 0.5)
            except (TypeError, ValueError):
                retry_after_seconds = 2.0
            time.sleep(retry_after_seconds)
            attempt += 1
            continue
        raise RuntimeError(
            f"{resolved_method} {url} failed with status {response.status_code}: "
            f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
        )


def _parse_iso_timestamp(value: Any) -> float | None:
    text = _safe_text(value)
    if not text:
        return None
    normalised = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_transient_workflow_error_text(value: Any) -> bool:
    lowered = _safe_text(value).lower()
    return any(marker in lowered for marker in TRANSIENT_WORKFLOW_ERROR_MARKERS)


def _should_retry_failed_instance(instance_payload: Mapping[str, Any]) -> bool:
    status = _safe_text(instance_payload.get("status"))
    if status != "failed":
        return False
    retry_count = int(instance_payload.get("retry_count") or 0)
    max_retries = int(instance_payload.get("max_retries") or 0)
    if max_retries > 0 and retry_count >= max_retries:
        return False
    return _is_transient_workflow_error_text(instance_payload.get("error"))


def _retry_failed_instance(
    *,
    session: requests.Session,
    base_url: str,
    instance_id: str,
) -> dict[str, Any]:
    url = f"{base_url}/api/workflows/instances/{instance_id}/retry"
    for attempt in range(1, 5):
        response = session.post(url, timeout=120)
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise RuntimeError(
                f"POST {url} returned non-JSON payload (status={response.status_code}): "
                f"{response.text[:500]}"
            ) from exc
        if response.status_code == 200:
            return payload
        retryable = (
            response.status_code == 503
            and bool(payload.get("retryable"))
            and attempt < 4
        )
        if retryable:
            retry_after_raw = payload.get("retry_after_seconds")
            try:
                retry_after_seconds = max(float(retry_after_raw), 0.5)
            except (TypeError, ValueError):
                retry_after_seconds = 2.0
            time.sleep(retry_after_seconds)
            continue
        raise RuntimeError(
            f"POST {url} failed with status {response.status_code}: "
            f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
        )
    raise RuntimeError(f"POST {url} exhausted retry budget without success.")


def _wait_for_workflow_instance_completion(
    *,
    session: requests.Session,
    base_url: str,
    instance_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> tuple[dict[str, Any], int]:
    deadline = time.time() + max(float(timeout_seconds), 30.0)
    instance_payload: dict[str, Any] | None = None
    retry_attempts = 0
    while time.time() < deadline:
        instance_payload = _request_json(
            session,
            "GET",
            f"{base_url}/api/workflows/instances/{instance_id}",
        )
        status = _safe_text(instance_payload.get("status"))
        if _should_retry_failed_instance(instance_payload):
            _retry_failed_instance(
                session=session,
                base_url=base_url,
                instance_id=instance_id,
            )
            retry_attempts += 1
            time.sleep(max(float(poll_interval_seconds), 0.2))
            continue
        if status in {"completed", "failed", "cancelled"}:
            break
        time.sleep(max(float(poll_interval_seconds), 0.2))

    _assert(
        instance_payload is not None,
        "Workflow instance polling did not yield a payload.",
    )
    return instance_payload or {}, retry_attempts


def _list_workflow_instances(
    *,
    session: requests.Session,
    base_url: str,
    workflow_id: str,
    limit: int = 5,
    source_event_type: str | None = None,
    source_event_id: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "workflow_id": workflow_id,
        "limit": max(int(limit), 1),
    }
    if _safe_text(source_event_type):
        params["source_event_type"] = _safe_text(source_event_type)
    if _safe_text(source_event_id):
        params["source_event_id"] = _safe_text(source_event_id)
    payload = _request_json(
        session,
        "GET",
        f"{base_url}/api/workflows/instances",
        params=params,
    )
    return [
        dict(item)
        for item in _as_list(payload.get("items"))
        if isinstance(item, Mapping)
    ]


def _select_recent_instance(
    instances: Sequence[Mapping[str, Any]],
    *,
    not_before_epoch: float | None,
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    selected_timestamp = float("-inf")
    grace_seconds = 30.0
    for raw in instances:
        instance = dict(raw)
        created_at = _parse_iso_timestamp(instance.get("created_at"))
        if (
            not_before_epoch is not None
            and created_at is not None
            and created_at < (not_before_epoch - grace_seconds)
        ):
            continue
        ranking_timestamp = (
            created_at
            if created_at is not None
            else _parse_iso_timestamp(instance.get("started_at")) or float("-inf")
        )
        if ranking_timestamp >= selected_timestamp:
            selected = instance
            selected_timestamp = ranking_timestamp
    return selected


def _find_ui_triggered_workflow_instance(
    *,
    session: requests.Session,
    base_url: str,
    workflow_id: str,
    source_event_id: str,
    submitted_at_epoch: float,
) -> dict[str, Any]:
    direct_matches = _list_workflow_instances(
        session=session,
        base_url=base_url,
        workflow_id=workflow_id,
        limit=5,
        source_event_type="chat_turn_workflow",
        source_event_id=source_event_id,
    )
    selected = _select_recent_instance(
        direct_matches,
        not_before_epoch=submitted_at_epoch,
    )
    if selected:
        return selected
    fallback_matches = _list_workflow_instances(
        session=session,
        base_url=base_url,
        workflow_id=workflow_id,
        limit=10,
    )
    return _select_recent_instance(
        fallback_matches,
        not_before_epoch=submitted_at_epoch,
    )


def _force_republish_testing_workflows() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    repo_root_text = str(repo_root)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)
    from src.backend.services.testing_workflow_vontology_service import (
        bootstrap_canonical_testing_workflows,
    )

    report = bootstrap_canonical_testing_workflows(force_republish=True)
    _assert(
        bool(report.get("success")),
        f"Force-republishing canonical testing workflows failed: {report!r}",
    )
    return report


def _extract_workflow_execution_entry(aux_calls: Sequence[Any]) -> dict[str, Any]:
    for entry in aux_calls:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("type") != "workflow_execution":
            continue
        if _safe_text(entry.get("workflow_id")) != ARXIV_INGESTION_TESTING_WORKFLOW_ID:
            continue
        return dict(entry)
    raise RuntimeError(
        "No workflow_execution aux entry found for the arXiv ingestion testing workflow."
    )


def _extract_pass_observations(observations: Sequence[Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in observations:
        if not isinstance(raw, Mapping):
            continue
        label = _safe_text(raw.get("label"))
        if not label:
            continue
        indexed[label] = dict(raw)
    return indexed


def _validate_result_payload(
    *,
    result_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_execution = _as_mapping(result_snapshot.get("workflow_execution"))
    _assert(
        _safe_text(workflow_execution.get("workflow_id"))
        == ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        f"Target workflow telemetry missing or mismatched: {workflow_execution!r}",
    )
    _assert(
        _safe_text(workflow_execution.get("final_status")) == "completed",
        f"Target workflow did not complete successfully: {workflow_execution!r}",
    )

    metadata_verification = _as_mapping(result_snapshot.get("metadata_verification"))
    for flag in EXPECTED_METADATA_FLAGS:
        _assert(
            metadata_verification.get(flag) is True,
            f"Metadata verification flag {flag!r} was not true: {metadata_verification!r}",
        )

    cleanup_summary = _as_mapping(result_snapshot.get("cleanup_summary"))
    _assert(
        cleanup_summary.get("cleanup_passed") is True,
        f"Cleanup did not pass: {cleanup_summary!r}",
    )
    _assert(
        not _as_list(cleanup_summary.get("failed_deletions")),
        f"Cleanup reported failed deletions: {cleanup_summary!r}",
    )

    observations = _extract_pass_observations(_as_list(result_snapshot.get("observations")))
    missing_labels = sorted(EXPECTED_OBSERVATION_LABELS.difference(observations))
    _assert(not missing_labels, f"Missing observation labels: {missing_labels}")
    for label in EXPECTED_OBSERVATION_LABELS:
        observation = observations[label]
        _assert(
            _safe_text(observation.get("verdict")) == "pass",
            f"Observation {label!r} did not pass: {observation!r}",
        )

    return {
        "workflow_execution": workflow_execution,
        "metadata_verification": metadata_verification,
        "cleanup_summary": cleanup_summary,
        "observations": observations,
    }


def _build_summary(
    *,
    result_snapshot: Mapping[str, Any],
    validation: Mapping[str, Any],
    instance_id: str | None = None,
) -> dict[str, Any]:
    workflow_execution = _as_mapping(validation.get("workflow_execution"))
    cleanup_summary = _as_mapping(validation.get("cleanup_summary"))
    observations = _as_mapping(validation.get("observations"))
    verdict = _safe_text(result_snapshot.get("verdict"))
    if not verdict:
        verdict = "pass"
    return {
        "status": "ok",
        "instance_id": _safe_text(instance_id) or None,
        "workflow_id": ARXIV_INGESTION_TESTING_WORKFLOW_ID,
        "target_workflow_id": _safe_text(workflow_execution.get("workflow_id")),
        "run_id": _safe_text(result_snapshot.get("run_id")),
        "verdict": verdict,
        "paper_concept_id": _safe_text(result_snapshot.get("paper_concept_id")),
        "file_copy_concept_id": _safe_text(result_snapshot.get("file_copy_concept_id")),
        "observation_labels": sorted(observations),
        "cleanup_deleted_concept_ids": _as_list(cleanup_summary.get("deleted_concept_ids")),
        "metadata_verification": _as_mapping(validation.get("metadata_verification")),
    }


def _run_workflow_api_test(
    *,
    session: requests.Session,
    base_url: str,
    prompt: str,
    workflow_user_id: str,
    workflow_org_id: str,
    workflow_namespace: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    create_payload = _request_json(
        session,
        "POST",
        f"{base_url}/api/workflows/instances",
        expected_status=201,
        json={
            "workflow_id": ARXIV_INGESTION_TESTING_WORKFLOW_ID,
            "user_id": workflow_user_id,
            "org_id": workflow_org_id,
            "namespace": workflow_namespace,
            "inputs": {"prompt": prompt},
        },
    )
    _assert(
        create_payload.get("success") is True,
        f"Workflow instance creation did not succeed: {create_payload!r}",
    )
    launch_resolution = _as_mapping(
        _as_mapping(create_payload.get("verification")).get(
            "workflow_launch_input_resolution"
        )
    )
    _assert(
        _safe_text(launch_resolution.get("status")) == "resolved",
        f"Launch-input resolution did not succeed: {launch_resolution!r}",
    )
    instance_id = _safe_text(create_payload.get("instance_id"))
    _assert(
        bool(instance_id),
        f"Workflow instance creation did not return an instance_id: {create_payload!r}",
    )

    resolved_instance_payload, retry_attempts = _wait_for_workflow_instance_completion(
        session=session,
        base_url=base_url,
        instance_id=instance_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    _assert(
        _safe_text(resolved_instance_payload.get("status")) == "completed",
        f"Workflow instance did not complete successfully: {resolved_instance_payload!r}",
    )

    outputs = _as_mapping(resolved_instance_payload.get("outputs"))
    _assert(
        bool(outputs),
        f"Completed workflow instance did not expose outputs: {resolved_instance_payload!r}",
    )
    emitted_verdict = _safe_text(outputs.get("verdict"))
    if emitted_verdict:
        _assert(
            emitted_verdict == "pass",
            f"Workflow verdict was not pass: {outputs!r}",
        )
    validation = _validate_result_payload(result_snapshot=outputs)
    summary = _build_summary(
        result_snapshot=outputs,
        validation=validation,
        instance_id=instance_id,
    )
    summary["workflow_retry_count"] = retry_attempts
    return summary


def _run_ui_test(
    *,
    session: requests.Session,
    base_url: str,
    prompt: str,
    model: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
) -> dict[str, Any]:
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
            _safe_text(diag_payload.get("effective_user_concept_id")) == user_concept_id,
            f"UI header-authenticated identity was not accepted: {diag_payload!r}",
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
        json={"session_name": session_name},
    )
    _assert(
        bool(_safe_text(session_payload.get("session_id"))),
        "Session creation did not return a session_id.",
    )
    _request_json(session, "POST", f"{base_url}/von/reset", json={})

    generate_submission = _request_json(
        session,
        "POST",
        f"{base_url}/von/generate",
        timeout_seconds=max(float(timeout_seconds), 30.0),
        expected_status=202,
        json={"prompt": prompt, "model": model, "background": True},
    )
    generate_submitted_at_epoch = time.time()
    _assert(
        generate_submission.get("background") is True,
        f"UI background submission did not acknowledge background execution: {generate_submission!r}",
    )
    task_id = _safe_text(generate_submission.get("task_id"))
    _assert(bool(task_id), f"UI background submission did not return a task_id: {generate_submission!r}")

    deadline = time.time() + max(float(timeout_seconds), 30.0)
    last_status_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        last_status_payload = _request_json(
            session,
            "GET",
            f"{base_url}/von/api/task/status/{task_id}",
        )
        task_status = _safe_text(last_status_payload.get("status"))
        if task_status == "completed":
            break
        if task_status in {"failed", "cancelled"}:
            raise RuntimeError(
                "UI background task did not complete successfully: "
                f"{json.dumps(last_status_payload, ensure_ascii=True, sort_keys=True)}"
            )
        time.sleep(max(float(poll_interval_seconds), 0.2))

    _assert(
        isinstance(last_status_payload, dict),
        "UI background task polling did not yield a status payload.",
    )
    resolved_status_payload = last_status_payload or {}
    _assert(
        _safe_text(resolved_status_payload.get("status")) == "completed",
        f"UI background task did not complete before timeout: {resolved_status_payload!r}",
    )

    task_result_payload = _request_json(
        session,
        "GET",
        f"{base_url}/von/api/task/result/{task_id}",
    )
    generate_payload = _as_mapping(task_result_payload.get("result"))
    _assert(bool(generate_payload), f"UI background task result was empty: {task_result_payload!r}")

    llm_debug = _as_mapping(generate_payload.get("llm_debug"))
    if not llm_debug:
        llm_debug = {
            "aux_llm_calls": _as_list(generate_payload.get("aux_llm_calls")),
            "workflow_routing": _as_mapping(generate_payload.get("workflow_routing")),
            "response": generate_payload.get("response_text"),
        }
    workflow_routing = _as_mapping(llm_debug.get("workflow_routing"))
    execution_entry: dict[str, Any] = {}
    try:
        execution_entry = _extract_workflow_execution_entry(
            _as_list(llm_debug.get("aux_llm_calls"))
        )
    except RuntimeError:
        execution_entry = {}
    result_snapshot = _as_mapping(execution_entry.get("result_snapshot"))
    retry_attempts = 0
    instance_id = _safe_text(execution_entry.get("instance_id"))
    if not result_snapshot:
        if not instance_id:
            launched_instance = _find_ui_triggered_workflow_instance(
                session=session,
                base_url=base_url,
                workflow_id=ARXIV_INGESTION_TESTING_WORKFLOW_ID,
                source_event_id=task_id,
                submitted_at_epoch=generate_submitted_at_epoch,
            )
            instance_id = _safe_text(launched_instance.get("instance_id"))
        _assert(
            bool(instance_id),
            "UI run did not expose a testing-workflow result snapshot or a turn-linked "
            f"workflow instance. Routing={workflow_routing!r}, llm_debug={llm_debug!r}",
        )
        instance_payload, retry_attempts = _wait_for_workflow_instance_completion(
            session=session,
            base_url=base_url,
            instance_id=instance_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        _assert(
            _safe_text(instance_payload.get("status")) == "completed",
            f"UI-triggered workflow instance did not complete successfully: {instance_payload!r}",
        )
        result_snapshot = _as_mapping(instance_payload.get("outputs"))
    _assert(bool(result_snapshot), "Workflow execution aux entry did not include a structured result_snapshot.")
    emitted_verdict = _safe_text(result_snapshot.get("verdict"))
    if emitted_verdict:
        _assert(
            emitted_verdict == "pass",
            f"Workflow verdict was not pass: {result_snapshot!r}",
        )
    validation = _validate_result_payload(result_snapshot=result_snapshot)
    summary = _build_summary(
        result_snapshot=result_snapshot,
        validation=validation,
        instance_id=instance_id or None,
    )
    summary["background_task_id"] = task_id
    summary["workflow_retry_count"] = retry_attempts
    summary["workflow_routing_workflow_id"] = _safe_text(workflow_routing.get("workflow_id"))
    summary["response_preview"] = _safe_text(
        llm_debug.get("response") or generate_payload.get("response_text")
    )[:300]
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the canonical arXiv ingestion testing workflow against a live Von server.",
    )
    parser.add_argument("--mode", choices=("workflow_api", "ui"), default="workflow_api")
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
            "Allow the test to target a server whose /health response does not "
            "report agent_test_instance=true. Use only when deliberately "
            "testing the interactive/user-facing server."
        ),
    )
    parser.add_argument("--arxiv-source", default=DEFAULT_ARXIV_SOURCE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=float, default=2700.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=3.0)
    parser.add_argument("--workflow-user-id", default=DEFAULT_WORKFLOW_USER_ID)
    parser.add_argument("--workflow-org-id", default=DEFAULT_WORKFLOW_ORG_ID)
    parser.add_argument("--workflow-namespace", default=DEFAULT_WORKFLOW_NAMESPACE)
    parser.add_argument("--user-concept-id", default=DEFAULT_UI_USER_CONCEPT_ID)
    parser.add_argument("--organisation-concept-id", default="")
    parser.add_argument(
        "--no-force-republish-testing-workflows",
        action="store_true",
        help="Skip the local force-republish of canonical testing workflow definitions before exercising the live server.",
    )
    parser.add_argument(
        "--session-name",
        default="JVNAUTOSCI-1570 arXiv ingestion acceptance",
    )
    args = parser.parse_args(argv)

    base_url = resolve_live_test_base_url(args.base_url)
    arxiv_source = _safe_text(args.arxiv_source) or DEFAULT_ARXIV_SOURCE
    prompt = (
        "Run the arXiv paper ingestion testing workflow on "
        f"{arxiv_source}. Use the workflow itself to verify title, authors, "
        "abstract, publication date, provenance, and cleanup."
    )

    bootstrap_summary: dict[str, Any] | None = None
    if not bool(args.no_force_republish_testing_workflows):
        bootstrap_summary = _force_republish_testing_workflows()

    session = requests.Session()
    _require_agent_test_server(
        session=session,
        base_url=base_url,
        allow_non_agent_test_server=bool(args.allow_non_agent_test_server),
    )
    if args.mode == "workflow_api":
        summary = _run_workflow_api_test(
            session=session,
            base_url=base_url,
            prompt=prompt,
            workflow_user_id=_safe_text(args.workflow_user_id) or DEFAULT_WORKFLOW_USER_ID,
            workflow_org_id=_safe_text(args.workflow_org_id) or DEFAULT_WORKFLOW_ORG_ID,
            workflow_namespace=(
                _safe_text(args.workflow_namespace) or DEFAULT_WORKFLOW_NAMESPACE
            ),
            timeout_seconds=float(args.timeout_seconds),
            poll_interval_seconds=float(args.poll_interval_seconds),
        )
    else:
        summary = _run_ui_test(
            session=session,
            base_url=base_url,
            prompt=prompt,
            model=_safe_text(args.model) or DEFAULT_MODEL,
            timeout_seconds=float(args.timeout_seconds),
            poll_interval_seconds=float(args.poll_interval_seconds),
            user_concept_id=_safe_text(args.user_concept_id) or DEFAULT_UI_USER_CONCEPT_ID,
            organisation_concept_id=_safe_text(args.organisation_concept_id),
            session_name=_safe_text(args.session_name) or "JVNAUTOSCI-1570 arXiv ingestion acceptance",
        )

    if bootstrap_summary is not None:
        summary["bootstrap_summary"] = {
            "success": bool(bootstrap_summary.get("success")),
            "publication": _as_mapping(bootstrap_summary.get("publication")),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


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
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
