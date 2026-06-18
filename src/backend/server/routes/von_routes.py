from flask import (
    Blueprint,
    request,
    jsonify,
    render_template,
    current_app,
    session,
    send_file,
    make_response,
)
import os
import re
import time
import threading
import secrets
import uuid
import json
import hashlib
from pathlib import Path
from urllib.parse import unquote
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Mapping, Sequence, cast
from ...security.visibility_predicates import CANONICAL_SPECIFIC_TO_USER_PREDICATE
from ...workflows.durable.registry_factory import build_workflow_registry_read_only
from ...workflows.durable.startup import get_instance_manager
from ...workflows.durable.models import WorkflowInstanceStatus
from ...workflows.durable.workflow_instance_submission_service import (
    submit_verified_workflow_instance,
)
from ...workflows.durable.turn_execution_runtime_support import (
    strip_completion_ledger_suffix,
)
from ...languagemodels.llm_interface import (
    _extract_ollama_model_id,
    _extract_openai_model_id,
    _looks_like_ollama_model,
    _looks_like_openai_model,
    get_active_model_name,
    get_llm_client,
)
from ...integrations.internal_mcp import (
    CancellationRequested,
    ProgressTracker,
    ToolCallParsingError,
)
from ...services import chat_history_service
from ...services import chat_prompt_queue_service
from ...services.background_task_service import background_task_registry
from ...services.live_request_load import (
    decrement_live_turns,
    increment_live_turns,
)
from ...services.coding_agent_identity_bootstrap_service import (
    CODING_AGENT_TYPE_ID,
    VON_SYSTEM_ID,
)
from ...services.window_session_context_service import (
    set_window_organisation,
    clear_window_organisation,
    get_effective_context,
)
from ...services.settings_service import (
    get_internal_mcp_max_tool_invocations,
    get_internal_mcp_tool_batch_cap,
    get_show_tool_use_during_thinking,
    get_buttonify_model_enabled,
)
from ...services.feature_flags import (
    get_display_elements_screen_fence_compat_enabled,
)
from ...services.chat_concept_reference_service import (
    build_context_concept_reference_metadata,
)
from ...services.buttonify_service import (
    BUTTONIFY_PROMPT_IDS,
    enforce_buttonify_prompt_contract,
    parse_buttonify_options_json_with_telemetry,
    sanitise_buttonify_options_with_telemetry,
)
from ...services.prompt_template_service import PromptTemplateService
from ...services.display_elements_service import (
    build_canonical_table_payload_from_records,
    build_turn_display_elements,
)
from ...services.turn_output_health_service import (
    build_turn_output_health,
    turn_output_health_issue_messages,
)
from ...services.response_transformation_telemetry import (
    build_response_transformation_event,
    build_response_transformation_telemetry_payload,
    record_response_transformation_event,
)
from ...services.turn_execution_diagnostic_event_service import (
    derive_tool_observations_from_diagnostic_events,
    update_tool_observation_summary,
)
from ...services.tool_evidence_projection_service import (
    project_nested_workflow_progress_evidence,
    project_surfaceable_concept_evidence,
    render_surfaceable_concept_lines,
)
from ...services.runtime_code_version_service import (
    get_runtime_code_version_info,
)
from ...services.tool_progress_store_service import (
    delete_tool_progress_state,
    discard_queued_tool_progress_state,
    fetch_tool_progress_state,
    queue_tool_progress_state_persistence,
)
from ...services.turn_timing_telemetry_service import (
    TurnTimingRecorder,
    build_turn_timing_trace,
    merge_timing_spans,
)
from ...services.turn_execution_record_service import (
    build_search_tool_evidence,
    build_turn_execution_record,
    build_workflow_routing_diagnostics,
)
from ...services.python_decision_authority_service import (
    annotate_python_decision_event,
    build_stage_authority_summary,
)
from ...workflows import (
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    WorkflowExecutionTrace,
    insert_workflow_execution_trace,
)
from ...workflows.conversation_turn_stage_model import (
    build_conversation_turn_stage_model_snapshot,
    build_conversation_turn_stage_path,
)
from ...workflows.llm_call_telemetry import (
    stamp_llm_call_timestamps as _stamp_llm_call_timestamps,
)
from .generate_route_support import (
    _GenerateConversationTurnInstanceState,
    _build_generate_error_body,
    _build_generate_error_debug_info,
    _build_generate_success_body,
    _finalise_generate_conversation_turn_instance,
    _persist_generate_turn_messages,
    _submit_generate_conversation_turn_instance,
)

# NOTE: Previous relative template_folder path ('../../frontend/...') was incorrect.
# From this file (src/backend/server/routes/von_routes.py) we need to traverse up THREE levels
# to reach the 'src' directory, then descend into frontend/web/von_interface/templates
# would raise TemplateNotFound for 'von_interface.html'.
_TEMPLATE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "../../../frontend/web/von_interface/templates"
    )
)
von_bp = Blueprint("von", __name__, template_folder=_TEMPLATE_DIR)


# --- Live request-load signal (JVNAUTOSCI-2383) ---------------------------
#
# Track in-flight *foreground* /von/generate turns so the in-process durable
# workflow worker can defer heavy background evaluation work while a user is
# actively waiting for an answer. Only the synchronous (non-background) generate
# path is counted: background submissions return immediately and are executed by
# their own machinery, so they do not represent a user blocked on the request
# thread. The teardown hook always runs, so the counter is balanced even when a
# handler raises.

_LIVE_TURN_FLAG_ATTR = "_von_live_turn_counted"


@von_bp.before_request
def _mark_live_foreground_turn() -> None:
    if request.endpoint != "von.generate" or request.method != "POST":
        return
    try:
        payload = request.get_json(silent=True)
    except Exception:
        payload = None
    background_mode = (
        bool(payload.get("background", False))
        if isinstance(payload, Mapping)
        else False
    )
    if background_mode:
        return
    increment_live_turns()
    setattr(request, _LIVE_TURN_FLAG_ATTR, True)


@von_bp.teardown_request
def _clear_live_foreground_turn(_exc: BaseException | None = None) -> None:
    if getattr(request, _LIVE_TURN_FLAG_ATTR, False):
        decrement_live_turns()


# ----------------- Tool progress (JVNAUTOSCI-942) -----------------

_TOOL_PROGRESS_TTL_SEC = 10 * 60
_TOOL_PROGRESS_LOCK = threading.Lock()
_TOOL_PROGRESS: dict[tuple[str, str], dict[str, Any]] = {}
_TOOL_PROGRESS_TERMINAL_STATUSES = {
    "completed",
    "follow_up_required",
    "error",
    "cancelled",
}
_TOOL_PROGRESS_TERMINAL_PHASES = {
    "completed",
    "follow_up_required",
    "failed",
    "error",
    "cancelled",
    "terminated",
}
_TOOL_PROGRESS_DIAGNOSTIC_EVENT_LIMIT = 80
_TOOL_PROGRESS_PHASE_HISTORY_LIMIT = 80
_TOOL_PROGRESS_SELECTED_WORKFLOW_EVENT_LIMIT = 80

_ONBOARDING_WORKFLOW_IDS_ENV = "VON_NEW_MEMBER_ONBOARDING_WORKFLOW_IDS"
_ONBOARDING_WORKFLOW_KEYWORDS = ("onboard", "onboarding")
_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT = 40
_TURN_EXECUTION_DIAGNOSTICS_PROMPT_PREVIEW_LIMIT = 1000
_THINKING_LARGE_LLM_PROMPT_CHAR_THRESHOLD = 60_000
_RECOVERY_STAGE_IDS = {
    "recovery_decision",
    "recovery_retry_prepare",
    "recovery_tool_batch_execute",
    "recovery_answer_prepare",
    "recovery_follow_up_prepare",
}
_RECOVERY_STAGE_ALIASES = {
    "apply_recovery_retry": "recovery_retry_prepare",
    "apply_recovery_tool_batch": "recovery_tool_batch_execute",
    "apply_recovery_answer": "recovery_answer_prepare",
    "apply_recovery_follow_up": "recovery_follow_up_prepare",
}
_DIAGNOSTIC_EXPORT_STRING_LIMIT = 2000
_DIAGNOSTIC_EXPORT_COLLECTION_LIMIT = 80
_DIAGNOSTIC_EXPORT_MAX_DEPTH = 8
_DIAGNOSTIC_EXPORT_REDACTED_VALUE = "[redacted]"
_DIAGNOSTIC_EXPORT_SUMMARISED_VALUE = "[summarised]"
_DIAGNOSTIC_EXPORT_REPO_ROOT = Path(__file__).resolve().parents[4]
_DIAGNOSTIC_EXPORT_RELATIVE_PATH = Path("data") / "diagnostic_latest.json"
_DIAGNOSTIC_EXPORT_FILE_PATH = (
    _DIAGNOSTIC_EXPORT_REPO_ROOT / _DIAGNOSTIC_EXPORT_RELATIVE_PATH
)
_DIAGNOSTIC_EXPORT_EXACT_SENSITIVE_KEYS = {
    "message",
    "messages",
    "content",
    "raw_content",
    "prompt",
    "prompt_raw",
    "prompt_preview",
    "user_prompt",
    "response",
    "screen",
    "spoken",
    "screen_text",
    "spoken_text",
    "authorization",
    "api_key",
    "apikey",
    "secret",
    "password",
    "cookie",
    "cookies",
    "set_cookie",
    "id_token",
    "access_token",
    "refresh_token",
}
_DIAGNOSTIC_EXPORT_STRUCTURAL_ONLY_KEYS = {
    "arguments",
    "args",
    "effective_arguments",
    "effective_payload",
    "payload",
    "request_payload",
    "response_payload",
    "raw_payload",
    "search_evidence",
    "tool_payload",
}


def _env_int(name: str, default: int, *, min_value: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = int(default)
    return max(min_value, value)


def _safe_log_preview(value: object, *, limit: int = 100) -> str:
    text = value if isinstance(value, str) else str(value)
    preview = text[: max(0, int(limit))]
    return preview.encode("ascii", "backslashreplace").decode("ascii")


def _safe_app_log(level: str, message: str, *args: object) -> None:
    try:
        logger = getattr(current_app, "logger", None)
        method = getattr(logger, level, None) if logger is not None else None
        if callable(method):
            method(message, *args)
    except Exception:
        # Logging must never be load-bearing for request success on Windows.
        pass


_TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC = _env_int(
    "VON_TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC", 5, min_value=1
)
_TOOL_PROGRESS_WAITING_THRESHOLD_SEC = _env_int(
    "VON_TOOL_PROGRESS_WAITING_THRESHOLD_SEC", 12, min_value=2
)
_TOOL_PROGRESS_STALL_THRESHOLD_SEC = _env_int(
    "VON_TOOL_PROGRESS_STALL_THRESHOLD_SEC", 45, min_value=5
)
if _TOOL_PROGRESS_WAITING_THRESHOLD_SEC >= _TOOL_PROGRESS_STALL_THRESHOLD_SEC:
    _TOOL_PROGRESS_STALL_THRESHOLD_SEC = (
        _TOOL_PROGRESS_WAITING_THRESHOLD_SEC + _TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC
    )

_TOOL_PROGRESS_WINDOW_SCOPE_PREFIX = "anon:window:"
_TOOL_PROGRESS_SESSION_SCOPE_PREFIX = "anon:session:"
_WINDOW_SESSION_HEADER_NAME = "X-Von-Window-Session"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _progress_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _progress_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _normalise_progress_fact_value(value: Any, *, limit: int = 320) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = re.sub(r"\s+", " ", value).strip()
        if not cleaned:
            return None
        if len(cleaned) <= limit:
            return cleaned
        return f"{cleaned[: max(0, limit - 3)].rstrip()}..."
    if isinstance(value, list):
        items = []
        for item in value[:5]:
            normalised = _normalise_progress_fact_value(item, limit=limit)
            if normalised is not None:
                items.append(normalised)
        return items or None
    return None


def _normalise_progress_facts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        raw_facts = value.get("facts")
    else:
        raw_facts = value
    if not isinstance(raw_facts, list):
        return []

    facts: list[dict[str, Any]] = []
    for raw_fact in raw_facts:
        if not isinstance(raw_fact, Mapping):
            continue
        fact_id = _progress_str(raw_fact.get("fact_id")) or _progress_str(
            raw_fact.get("id")
        )
        label = _progress_str(raw_fact.get("label"))
        if not fact_id or not label:
            continue
        status = _progress_str(raw_fact.get("status")) or (
            "available" if "value" in raw_fact else "missing"
        )
        fact: dict[str, Any] = {
            "schema_version": _progress_str(raw_fact.get("schema_version"))
            or "workflow_progress_projection.v1",
            "fact_id": fact_id,
            "label": label,
            "status": status,
            "present": bool(raw_fact.get("present")),
            "redacted": bool(raw_fact.get("redacted")),
            "truncated": bool(raw_fact.get("truncated")),
        }
        for key in (
            "value_kind",
            "visibility",
            "source_path",
            "resolved_path",
            "contract_id",
            "redaction_policy",
            "reason_code",
            "workflow_id",
            "state_id",
            "action_id",
        ):
            text_value = _progress_str(raw_fact.get(key))
            if text_value:
                fact[key] = text_value
        if "value" in raw_fact and not fact["redacted"]:
            normalised_value = _normalise_progress_fact_value(raw_fact.get("value"))
            if normalised_value is not None:
                fact["value"] = normalised_value
        facts.append(fact)
        if len(facts) >= 12:
            break
    return facts


def _normalise_selected_workflow_execution_event(
    update: Mapping[str, Any],
    *,
    sequence_no: int,
    at_utc: str,
) -> dict[str, Any] | None:
    raw_event = update.get("selected_workflow_execution_event")
    if not isinstance(raw_event, Mapping):
        return None

    event = {
        str(key): value for key, value in raw_event.items() if isinstance(key, str)
    }
    status = _progress_str(event.get("status")) or _progress_str(update.get("status"))
    event_kind = _progress_str(event.get("event_kind")) or status
    workflow_id = (
        _progress_str(event.get("workflow_id"))
        or _progress_str(update.get("workflow_id"))
        or _progress_str(update.get("selected_workflow_id"))
    )
    if not status or not event_kind or not workflow_id:
        return None

    normalised: dict[str, Any] = {
        "schema_version": "selected_workflow_execution_event.v1",
        "sequence_no": sequence_no,
        "at_utc": at_utc,
        "status": status,
        "event_kind": event_kind,
        "workflow_id": workflow_id,
    }
    for key in (
        "selected_workflow_id",
        "selected_workflow_name",
        "selected_execution_mode",
        "state_id",
        "action_id",
        "action_status",
        "action_outcome",
        "outcome",
        "final_state",
        "error",
        "execution_mode",
        "result_summary",
    ):
        value = _progress_str(event.get(key)) or _progress_str(update.get(key))
        if value:
            normalised[key] = value
    state_attempt = _progress_number(event.get("state_attempt"))
    if state_attempt is not None:
        normalised["state_attempt"] = int(max(0.0, state_attempt))
    duration_ms = _progress_number(event.get("duration_ms"))
    if duration_ms is not None:
        normalised["duration_ms"] = int(max(0.0, duration_ms))
    progress_facts = _normalise_progress_facts(
        event.get("progress_facts") or update.get("progress_facts")
    )
    if progress_facts:
        normalised["progress_facts"] = progress_facts
    return normalised


def _append_selected_workflow_execution_event(
    existing_execution: Any,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    existing_payload = (
        existing_execution if isinstance(existing_execution, Mapping) else {}
    )
    existing_events = existing_payload.get("events")
    events = (
        [dict(item) for item in existing_events if isinstance(item, Mapping)]
        if isinstance(existing_events, list)
        else []
    )
    events.append(dict(event))
    events = events[-_TOOL_PROGRESS_SELECTED_WORKFLOW_EVENT_LIMIT:]

    workflow_id = (
        _progress_str(event.get("selected_workflow_id"))
        or _progress_str(event.get("workflow_id"))
        or _progress_str(existing_payload.get("selected_workflow_id"))
        or _progress_str(existing_payload.get("workflow_id"))
    )
    payload: dict[str, Any] = {
        "schema_version": "selected_workflow_execution.v1",
        "workflow_id": workflow_id,
        "selected_workflow_id": workflow_id,
        "event_count": int(_progress_number(existing_payload.get("event_count")) or 0)
        + 1,
        "latest_event": dict(event),
        "events": events,
    }
    for key in ("selected_workflow_name", "selected_execution_mode"):
        value = _progress_str(event.get(key)) or _progress_str(
            existing_payload.get(key)
        )
        if value:
            payload[key] = value
    return payload


def _get_current_chat_prompt_queue_scope() -> dict[str, str | None] | tuple[Any, int]:
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Not authenticated",
                    "error_code": "not_authenticated",
                }
            ),
            401,
        )

    window_session_id = request.headers.get(_WINDOW_SESSION_HEADER_NAME)
    effective_context = get_effective_context(
        window_session_id,
        dict(session),
        user_concept_id.strip(),
    )
    organisation_concept_id = (
        effective_context.get("organisation_id")
        or session.get("org_id")
        or session.get("organisation_concept_id")
    )
    namespace = effective_context.get("namespace") or session.get("namespace")
    return chat_prompt_queue_service.build_queue_scope(
        user_concept_id=user_concept_id.strip(),
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )


def _chat_prompt_queue_payload() -> dict[str, Any]:
    try:
        payload = request.get_json(silent=True)
    except Exception:
        payload = None
    return payload if isinstance(payload, dict) else {}


def _chat_prompt_queue_scope_log_summary(
    scope: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(scope, Mapping):
        return {}

    def fingerprint(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]

    return {
        "user": fingerprint(scope.get("user_concept_id")),
        "organisation": fingerprint(scope.get("organisation_concept_id")),
        "namespace": fingerprint(scope.get("namespace")),
    }


def _chat_prompt_queue_error_response(
    exc: Exception,
    *,
    action: str | None = None,
    queue_id: str | None = None,
    scope: Mapping[str, Any] | None = None,
):
    if isinstance(exc, chat_prompt_queue_service.InvalidChatPromptQueueInput):
        return (
            jsonify(
                {
                    "success": False,
                    "error": str(exc),
                    "error_code": "invalid_input",
                }
            ),
            400,
        )
    if isinstance(exc, chat_prompt_queue_service.ChatPromptQueueRecordNotFound):
        current_app.logger.warning(
            "Chat prompt queue transition miss action=%s queue_id=%s code=%s details=%s scope=%s",
            action,
            queue_id,
            exc.error_code,
            exc.details,
            _chat_prompt_queue_scope_log_summary(scope),
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": str(exc),
                    "error_code": exc.error_code,
                    "details": exc.details,
                }
            ),
            404,
        )
    if isinstance(exc, chat_prompt_queue_service.ChatPromptQueueUnavailable):
        current_app.logger.warning(
            "Chat prompt queue unavailable action=%s queue_id=%s scope=%s",
            action,
            queue_id,
            _chat_prompt_queue_scope_log_summary(scope),
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": str(exc),
                    "error_code": "backend_unavailable",
                }
            ),
            503,
        )
    current_app.logger.exception("Chat prompt queue route failed")
    return (
        jsonify(
            {
                "success": False,
                "error": "Internal server error",
                "error_code": "internal_error",
            }
        ),
        500,
    )


@von_bp.route("/api/chat_prompt_queue", methods=["GET"])
def list_chat_prompt_queue_route():
    scope = _get_current_chat_prompt_queue_scope()
    if isinstance(scope, tuple):
        return scope
    try:
        records = chat_prompt_queue_service.list_active_queue_records(scope=scope)
        return jsonify({"success": True, "items": records})
    except Exception as exc:
        return _chat_prompt_queue_error_response(exc, action="list", scope=scope)


@von_bp.route("/api/chat_prompt_queue", methods=["POST"])
def create_chat_prompt_queue_route():
    scope = _get_current_chat_prompt_queue_scope()
    if isinstance(scope, tuple):
        return scope
    payload = _chat_prompt_queue_payload()
    status = (
        chat_prompt_queue_service.STATUS_IN_PROGRESS
        if payload.get("status") == chat_prompt_queue_service.STATUS_IN_PROGRESS
        else chat_prompt_queue_service.STATUS_QUEUED
    )
    source = (
        "active" if status == chat_prompt_queue_service.STATUS_IN_PROGRESS else "queued"
    )
    try:
        record = chat_prompt_queue_service.create_queue_record(
            scope=scope,
            prompt_raw=payload.get("prompt_raw"),
            session_id=payload.get("session_id"),
            session_name=payload.get("session_name"),
            status=status,
            source=source,
        )
        return jsonify({"success": True, "item": record}), 201
    except Exception as exc:
        return _chat_prompt_queue_error_response(exc, action="create", scope=scope)


@von_bp.route("/api/chat_prompt_queue/<queue_id>", methods=["PATCH"])
def update_chat_prompt_queue_route(queue_id: str):
    scope = _get_current_chat_prompt_queue_scope()
    if isinstance(scope, tuple):
        return scope
    payload = _chat_prompt_queue_payload()
    try:
        record = chat_prompt_queue_service.update_queued_record(
            scope=scope,
            queue_id=queue_id,
            prompt_raw=payload.get("prompt_raw"),
            session_id=payload.get("session_id"),
            session_name=payload.get("session_name"),
        )
        return jsonify({"success": True, "item": record})
    except Exception as exc:
        return _chat_prompt_queue_error_response(
            exc,
            action="update",
            queue_id=queue_id,
            scope=scope,
        )


@von_bp.route("/api/chat_prompt_queue/<queue_id>", methods=["DELETE"])
def cancel_chat_prompt_queue_route(queue_id: str):
    scope = _get_current_chat_prompt_queue_scope()
    if isinstance(scope, tuple):
        return scope
    try:
        record = chat_prompt_queue_service.cancel_prompt_record(
            scope=scope,
            queue_id=queue_id,
        )
        return jsonify({"success": True, "item": record})
    except Exception as exc:
        return _chat_prompt_queue_error_response(
            exc,
            action="cancel",
            queue_id=queue_id,
            scope=scope,
        )


@von_bp.route("/api/chat_prompt_queue/<queue_id>/claim", methods=["POST"])
def claim_chat_prompt_queue_route(queue_id: str):
    scope = _get_current_chat_prompt_queue_scope()
    if isinstance(scope, tuple):
        return scope
    try:
        record = chat_prompt_queue_service.claim_queue_record(
            scope=scope,
            queue_id=queue_id,
        )
        return jsonify({"success": True, "item": record})
    except Exception as exc:
        return _chat_prompt_queue_error_response(
            exc,
            action="claim",
            queue_id=queue_id,
            scope=scope,
        )


@von_bp.route("/api/chat_prompt_queue/<queue_id>/requeue", methods=["POST"])
def requeue_chat_prompt_queue_route(queue_id: str):
    scope = _get_current_chat_prompt_queue_scope()
    if isinstance(scope, tuple):
        return scope
    try:
        record = chat_prompt_queue_service.requeue_prompt_record(
            scope=scope,
            queue_id=queue_id,
        )
        return jsonify({"success": True, "item": record})
    except Exception as exc:
        return _chat_prompt_queue_error_response(
            exc,
            action="requeue",
            queue_id=queue_id,
            scope=scope,
        )


@von_bp.route("/api/chat_prompt_queue/<queue_id>/finish", methods=["POST"])
def finish_chat_prompt_queue_route(queue_id: str):
    scope = _get_current_chat_prompt_queue_scope()
    if isinstance(scope, tuple):
        return scope
    payload = _chat_prompt_queue_payload()
    try:
        record = chat_prompt_queue_service.finish_prompt_record(
            scope=scope,
            queue_id=queue_id,
            status=payload.get("status") or chat_prompt_queue_service.STATUS_COMPLETED,
            error=payload.get("error"),
        )
        return jsonify({"success": True, "item": record})
    except Exception as exc:
        return _chat_prompt_queue_error_response(
            exc,
            action="finish",
            queue_id=queue_id,
            scope=scope,
        )


def _normalise_progress_goal_label(
    value: Any,
    *,
    limit: int = 160,
) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = re.sub(r"\s+", " ", value).strip()
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 3:
        return collapsed[:limit]
    return f"{collapsed[: limit - 3].rstrip()}..."


def _summarise_progress_goal_targets(values: Any) -> str | None:
    if not isinstance(values, (list, tuple)):
        return None
    targets: list[str] = []
    seen: set[str] = set()
    for value in values:
        target = _normalise_progress_goal_label(value, limit=80)
        if not target:
            continue
        lowered = target.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        targets.append(target)
        if len(targets) >= 2:
            break
    if not targets:
        return None
    return ", ".join(targets)


def _build_continuation_progress_goal_label(
    continuation_context: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(continuation_context, Mapping):
        return None
    if continuation_context.get("applied") is not True:
        return None

    unresolved_effects = continuation_context.get("unresolved_required_effects")
    if isinstance(unresolved_effects, list):
        for effect in unresolved_effects:
            if not isinstance(effect, Mapping):
                continue
            description = _normalise_progress_goal_label(effect.get("description"))
            status_reason = _normalise_progress_goal_label(effect.get("status_reason"))
            effect_type = _progress_str(effect.get("effect_type"))
            target_text = _summarise_progress_goal_targets(effect.get("targets"))

            goal = description or status_reason
            if not goal and effect_type:
                goal = _normalise_progress_goal_label(
                    effect_type.replace("_", " ").strip()
                )
            if not goal:
                continue
            if target_text and target_text.lower() not in goal.lower():
                goal = _normalise_progress_goal_label(f"{goal} [{target_text}]")
            if goal:
                return goal

    selected_workflow_id = _progress_str(
        continuation_context.get("selected_workflow_id")
    )
    if selected_workflow_id:
        return _normalise_progress_goal_label(f"Continue {selected_workflow_id}")
    return None


def _build_progress_goal_label(
    *,
    prompt_text: str | None,
    continuation_context: Mapping[str, Any] | None = None,
) -> str | None:
    continuation_goal = _build_continuation_progress_goal_label(continuation_context)
    if continuation_goal:
        return continuation_goal
    return _normalise_progress_goal_label(prompt_text)


def _default_stage_label(stage: str) -> str:
    mapping = {
        "context_build": "Understanding request",
        "context_adjudication": "Adjudicating prior context",
        "context_adjudication_decision": "Adjudicating prior context",
        "expected_outcome_inference": "Inferring success criteria",
        "workflow_discovery": "Looking for relevant workflows",
        "workflow_discovery_complete": "Evaluating workflow applicability",
        "workflow_dispatch_prepare": "Preparing workflow dispatch",
        "workflow_dispatch": "Selecting workflow",
        "selector_preparation": "Preparing selector context",
        "selector_decision": "Selecting workflow",
        "tool_plan": "Deciding next actions",
        "tool_execute": "Applying actions",
        "screen_backfill": "Composing response",
        "narration": "Generating narration",
        "buttonify": "Generating quick replies",
        "response_finalising": "Finalising response",
        "tool_recovery": "Recovering tool call",
        "recovery_decision": "Recovery decision",
        "recovery_retry_prepare": "Preparing recovery retry",
        "recovery_tool_batch_execute": "Executing recovery tools",
        "recovery_answer_prepare": "Preparing recovery answer",
        "recovery_follow_up_prepare": "Preparing recovery follow-up",
        "apply_recovery_retry": "Preparing recovery retry",
        "apply_recovery_tool_batch": "Executing recovery tools",
        "apply_recovery_answer": "Preparing recovery answer",
        "apply_recovery_follow_up": "Preparing recovery follow-up",
        "orchestrator_start": "Planning response approach",
        "orchestrator_end": "Finishing orchestration",
        "completed": "Complete",
        "follow_up_required": "Follow-up required",
        "error": "Error",
    }
    if stage in mapping:
        return mapping[stage]
    return stage.replace("_", " ").strip().title() or "Thinking"


def _derive_progress_stage(update: dict[str, Any], existing: dict[str, Any]) -> str:
    explicit_stage = _progress_str(update.get("stage"))
    if explicit_stage:
        return explicit_stage

    phase = _progress_str(update.get("phase"))
    if phase:
        return phase

    status = _progress_str(update.get("status")) or ""
    status_stage_map = {
        "orchestrator_start": "workflow_dispatch_prepare",
        "orchestrator_end": "orchestrator_end",
        "llm_request_prepared": "llm_call",
        "llm_call_start": "llm_call",
        "llm_call_chunk": "llm_call",
        "llm_call_end": "llm_call",
        "tool_call_start": "tool_execute",
        "tool_invoked": "tool_execute",
        "tool_failed": "tool_execute",
        "tool_blocked": "tool_execute",
        "retry_start": "tool_recovery",
        "retry_end": "tool_recovery",
        "completed": "completed",
        "follow_up_required": "follow_up_required",
        "error": "error",
    }
    derived = status_stage_map.get(status)
    if derived:
        return derived

    existing_stage = _progress_str(existing.get("stage"))
    if existing_stage:
        return existing_stage

    existing_phase = _progress_str(existing.get("phase"))
    if existing_phase:
        return existing_phase

    return "context_build"


def _derive_progress_event_kind(update: dict[str, Any]) -> str:
    explicit_kind = _progress_str(update.get("event_kind"))
    if explicit_kind:
        return explicit_kind

    status = _progress_str(update.get("status")) or ""
    status_kind_map = {
        "phase_transition": "stage_event",
        "heartbeat": "heartbeat",
        "orchestrator_start": "orchestrator_start",
        "orchestrator_end": "orchestrator_end",
        "llm_request_prepared": "llm_request_prepared",
        "llm_call_start": "llm_call_start",
        "llm_call_chunk": "llm_call_chunk",
        "llm_call_end": "llm_call_end",
        "tool_call_start": "tool_call_start",
        "tool_invoked": "tool_call_end",
        "tool_failed": "tool_call_end",
        "tool_blocked": "tool_call_end",
        "retry_start": "retry_start",
        "retry_end": "retry_end",
        "completed": "completed",
        "follow_up_required": "completed",
        "error": "error",
    }
    return status_kind_map.get(status, status or "status_update")


def _classify_progress_cause(stage: str, status: str) -> str:
    stage_lower = stage.lower()
    status_lower = status.lower()

    if "worker_unavailable" in status_lower:
        return "worker_unavailable"
    if "finalis" in stage_lower:
        return "post_processing"
    if "tool" in stage_lower or status_lower.startswith("tool_"):
        return "tool_timeout"
    if "llm" in stage_lower or status_lower.startswith("llm_"):
        return "model_timeout"
    if "retry" in stage_lower or status_lower.startswith("retry_"):
        return "model_timeout"
    if "workflow_dispatch_prepare" in stage_lower:
        return "orchestrator_startup_wait"
    if "orchestrator" in stage_lower:
        return "orchestrator_startup_wait"
    return "network_silence"


def _derive_progress_liveness(
    state: dict[str, Any], *, now_epoch: float | None = None
) -> dict[str, Any]:
    now = float(now_epoch if now_epoch is not None else time.time())

    status = (_progress_str(state.get("status")) or "thinking").lower()
    stage = _progress_str(state.get("stage")) or _progress_str(state.get("phase")) or ""

    last_activity_epoch = _progress_number(
        state.get("last_activity_epoch")
    ) or _progress_number(state.get("updated_at_epoch"))
    if last_activity_epoch is None:
        last_activity_epoch = now

    last_stage_activity_epoch = (
        _progress_number(state.get("last_stage_activity_epoch")) or last_activity_epoch
    )

    activity_idle_ms = int(max(0.0, (now - float(last_activity_epoch)) * 1000.0))
    stage_idle_ms = int(max(0.0, (now - float(last_stage_activity_epoch)) * 1000.0))

    if status in _TOOL_PROGRESS_TERMINAL_STATUSES:
        liveness_state = "active"
        liveness_reason = status
        stall_detected = False
    elif activity_idle_ms >= int(_TOOL_PROGRESS_STALL_THRESHOLD_SEC * 1000):
        liveness_state = "stalled"
        liveness_reason = _classify_progress_cause(stage, status)
        stall_detected = True
    elif stage_idle_ms >= int(_TOOL_PROGRESS_WAITING_THRESHOLD_SEC * 1000):
        liveness_state = "waiting"
        liveness_reason = _classify_progress_cause(stage, status)
        stall_detected = False
    else:
        liveness_state = "active"
        liveness_reason = "recent_activity"
        stall_detected = False

    return {
        "liveness_state": liveness_state,
        "liveness_reason": liveness_reason,
        "stall_detected": stall_detected,
        "waiting_threshold_sec": int(_TOOL_PROGRESS_WAITING_THRESHOLD_SEC),
        "stall_threshold_sec": int(_TOOL_PROGRESS_STALL_THRESHOLD_SEC),
        "activity_idle_ms": activity_idle_ms,
        "stage_idle_ms": stage_idle_ms,
    }


def _serialise_tool_progress_state(
    state: dict[str, Any], *, now_epoch: float | None = None
) -> dict[str, Any]:
    payload = dict(state)
    payload.update(_derive_progress_liveness(payload, now_epoch=now_epoch))

    payload.pop("updated_at_epoch", None)
    payload.pop("request_started_epoch", None)
    payload.pop("last_activity_epoch", None)
    payload.pop("last_stage_activity_epoch", None)

    events = payload.get("diagnostic_events")
    if isinstance(events, list):
        payload["diagnostic_events"] = list(
            events[-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT:]
        )

    workflow_stage_path = _extract_workflow_stage_path_from_progress(payload)
    if workflow_stage_path is None:
        workflow_stage_path = _build_live_workflow_stage_path(payload)
    payload["workflow_stage_path"] = workflow_stage_path
    diagnostic_events = [
        cast(dict[str, Any], entry)
        for entry in payload.get("diagnostic_events", [])
        if isinstance(entry, dict)
    ]
    phase_history = _extract_phase_history_from_progress_state(
        payload,
        diagnostic_events=diagnostic_events,
    )
    workflow_discovery = payload.get("workflow_discovery")
    if isinstance(workflow_discovery, Mapping):
        payload["workflow_discovery"] = _normalise_workflow_discovery_progress_payload(
            workflow_discovery
        )
    tool_history = _derive_tool_history_from_diagnostic_events(diagnostic_events)
    workflow_routing = payload.get("workflow_routing")
    workflow_routing_aux = payload.get("workflow_routing_aux")
    if isinstance(workflow_routing, Mapping) or isinstance(workflow_routing_aux, list):
        payload["workflow_routing_diagnostics"] = build_workflow_routing_diagnostics(
            workflow_discovery=(
                cast(dict[str, Any], payload["workflow_discovery"])
                if isinstance(payload.get("workflow_discovery"), Mapping)
                else None
            ),
            workflow_routing=(
                cast(dict[str, Any], workflow_routing)
                if isinstance(workflow_routing, Mapping)
                else None
            ),
            turn_execution_diagnostics={
                "latest_progress": payload,
                "phase_history": phase_history,
                "workflow_stage_path": workflow_stage_path,
            },
            aux_llm_calls=(
                [
                    cast(dict[str, Any], entry)
                    for entry in workflow_routing_aux
                    if isinstance(entry, dict)
                ]
                if isinstance(workflow_routing_aux, list)
                else None
            ),
        )
        payload.update(
            _resolve_workflow_selection_fields(
                latest_progress_payload=payload,
                workflow_routing_diagnostics=(
                    payload.get("workflow_routing_diagnostics")
                    if isinstance(payload.get("workflow_routing_diagnostics"), Mapping)
                    else None
                ),
            )
        )
    stage_diagnostics = _build_turn_execution_stage_diagnostics(
        diagnostic_events=diagnostic_events,
        workflow_stage_path=workflow_stage_path,
        tool_history=tool_history,
        tool_observation_summary=_extract_tool_observation_summary_from_progress(
            payload
        ),
        workflow_discovery=(
            cast(dict[str, Any], payload["workflow_discovery"])
            if isinstance(payload.get("workflow_discovery"), Mapping)
            else None
        ),
        latest_progress=payload,
        aux_llm_calls=(
            [
                cast(dict[str, Any], entry)
                for entry in workflow_routing_aux
                if isinstance(entry, dict)
            ]
            if isinstance(workflow_routing_aux, list)
            else None
        ),
    )
    payload["phase_history"] = phase_history
    payload["progress_events"] = _build_progress_events_from_phase_history(
        phase_history,
        latest_progress=payload,
    )
    payload["stage_diagnostics"] = stage_diagnostics
    payload["activity_history"] = _build_activity_history_from_phase_history(
        phase_history,
        stage_diagnostics=stage_diagnostics,
        latest_progress=payload,
    )
    recovery_progress = _build_thinking_recovery_progress_payload(payload)
    if recovery_progress:
        payload["recovery_progress"] = recovery_progress
    else:
        payload.pop("recovery_progress", None)
    payload["thinking_interpretability"] = _build_thinking_interpretability_payload(
        payload
    )
    llm_calls_for_timing = [
        cast(dict[str, Any], entry)
        for entry in (payload.get("llm_calls") or [])
        if isinstance(entry, dict)
    ]
    tool_invocations_for_timing = [
        cast(dict[str, Any], entry)
        for entry in (payload.get("tool_invocations") or [])
        if isinstance(entry, dict)
    ]
    extra_timing_spans = _extract_turn_timing_spans_from_payload(payload)
    elapsed_ms_value = None
    elapsed_raw = _progress_number(payload.get("elapsed_ms"))
    if elapsed_raw is not None:
        elapsed_ms_value = int(max(0.0, elapsed_raw))
    turn_timing_trace = build_turn_timing_trace(
        request_id=_progress_str(payload.get("request_id")),
        phase_history=phase_history,
        diagnostic_events=diagnostic_events,
        llm_calls=llm_calls_for_timing,
        tool_history=tool_history,
        tool_invocations=tool_invocations_for_timing,
        extra_spans=extra_timing_spans,
        elapsed_ms_value=elapsed_ms_value,
    )
    payload["turn_timing_trace"] = turn_timing_trace
    payload["timing_spans"] = list(turn_timing_trace.get("spans") or [])
    payload["timing_summary"] = {
        "schema_version": "turn_timing_summary.v1",
        "span_count": turn_timing_trace.get("span_count"),
        "stored_span_count": turn_timing_trace.get("stored_span_count"),
        "dropped_span_count": turn_timing_trace.get("dropped_span_count"),
        "summary": turn_timing_trace.get("summary"),
        "slowest_spans": list(turn_timing_trace.get("slowest_spans") or [])[:5],
        "model_prompt_summary": list(
            turn_timing_trace.get("model_prompt_summary") or []
        )[:5],
    }
    payload.pop("_workflow_runtime_stages", None)
    payload.pop("_phase_history", None)
    payload.pop("_timing_spans", None)
    payload.pop("_stage_summaries", None)

    return payload


def _build_tool_progress_compact_summary(
    state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None

    summary = state.get("diagnostic_summary")
    summary = summary if isinstance(summary, dict) else {}
    counters = state.get("counters")
    counters = counters if isinstance(counters, dict) else {}

    return {
        "request_id": state.get("request_id"),
        "sequence_no": state.get("sequence_no"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "goal_label": state.get("goal_label"),
        "subtask": state.get("subtask"),
        "elapsed_ms": state.get("elapsed_ms"),
        "idle_ms": state.get("activity_idle_ms", state.get("idle_ms")),
        "stage_idle_ms": state.get("stage_idle_ms"),
        "liveness_state": state.get("liveness_state"),
        "liveness_reason": state.get("liveness_reason"),
        "stall_detected": state.get("stall_detected"),
        "last_activity_at_utc": state.get("last_activity_at_utc"),
        "event_count": summary.get("event_count"),
        "counters": {
            "tokens_streamed": counters.get("tokens_streamed", 0),
            "tools_started": counters.get("tools_started", 0),
            "tools_completed": counters.get("tools_completed", 0),
        },
        "waiting_threshold_sec": state.get("waiting_threshold_sec"),
        "stall_threshold_sec": state.get("stall_threshold_sec"),
    }


def _extract_selected_workflow_identity_from_progress(
    payload: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    selected_workflow_id = _progress_str(payload.get("selected_workflow_id"))
    selected_workflow_name = _progress_str(payload.get("selected_workflow_name"))

    routing_diagnostics = payload.get("workflow_routing_diagnostics")
    if isinstance(routing_diagnostics, Mapping):
        selected_workflow_id = selected_workflow_id or _progress_str(
            routing_diagnostics.get("selected_workflow_id")
        )
        selected_workflow_name = selected_workflow_name or _progress_str(
            routing_diagnostics.get("selected_workflow_name")
        )
        dispatch = routing_diagnostics.get("dispatch")
        if isinstance(dispatch, Mapping):
            selected_workflow_id = selected_workflow_id or _progress_str(
                dispatch.get("dispatch_workflow_id")
            )

    selected_execution = payload.get("selected_workflow_execution")
    if isinstance(selected_execution, Mapping):
        selected_workflow_id = (
            selected_workflow_id
            or _progress_str(selected_execution.get("selected_workflow_id"))
            or _progress_str(selected_execution.get("workflow_id"))
        )
        selected_workflow_name = selected_workflow_name or _progress_str(
            selected_execution.get("selected_workflow_name")
        )

    return selected_workflow_id, selected_workflow_name


def _latest_non_finalising_stage_from_path(payload: Mapping[str, Any]) -> str | None:
    workflow_stage_path = payload.get("workflow_stage_path")
    if not isinstance(workflow_stage_path, Mapping):
        return None

    path = workflow_stage_path.get("path")
    if not isinstance(path, list):
        return None

    finalising_stages = {
        "response_finalising",
        "completed",
        "follow_up_required",
        "error",
    }
    for entry in reversed(path):
        if not isinstance(entry, Mapping):
            continue
        stage_id = _progress_str(entry.get("stage_id"))
        if not stage_id:
            continue
        if stage_id in finalising_stages:
            continue
        return stage_id
    return None


def _selected_workflow_evidence_sources(
    payload: Mapping[str, Any],
) -> list[str]:
    """Return the lineage of where selected-workflow evidence was resolved from.

    Supports the default-mode Thinking-card precedence contract (JVNAUTOSCI-2381)
    so the frontend and diagnostics can see why a selected-workflow execution row
    is required before auxiliary finalisation rows.
    """

    sources: list[str] = []

    def _note(source: str, present: bool) -> None:
        if present and source not in sources:
            sources.append(source)

    _note(
        "progress.selected_workflow_id",
        bool(_progress_str(payload.get("selected_workflow_id"))),
    )

    routing_diagnostics = payload.get("workflow_routing_diagnostics")
    if isinstance(routing_diagnostics, Mapping):
        _note(
            "workflow_routing_diagnostics.selected_workflow_id",
            bool(_progress_str(routing_diagnostics.get("selected_workflow_id"))),
        )
        dispatch = routing_diagnostics.get("dispatch")
        if isinstance(dispatch, Mapping):
            _note(
                "workflow_routing_diagnostics.dispatch.dispatch_workflow_id",
                bool(_progress_str(dispatch.get("dispatch_workflow_id"))),
            )

    selected_execution = payload.get("selected_workflow_execution")
    if isinstance(selected_execution, Mapping):
        _note(
            "selected_workflow_execution",
            bool(
                _progress_str(selected_execution.get("selected_workflow_id"))
                or _progress_str(selected_execution.get("workflow_id"))
            ),
        )

    workflow_stage_path = payload.get("workflow_stage_path")
    if isinstance(workflow_stage_path, Mapping):
        path = workflow_stage_path.get("path")
        _note(
            "workflow_stage_path",
            isinstance(path, list) and len(path) > 0,
        )

    return sources


def _build_thinking_progress_precedence_contract(
    *,
    selected_workflow_id: str | None,
    has_tool_evidence: bool,
    blocker_summary: str | None,
    post_processing_only: bool,
    evidence_sources: Sequence[str],
) -> dict[str, Any]:
    """Build the canonical default-mode Thinking-card row precedence contract.

    The default Thinking-card view must foreground the selected workflow's
    execution before auxiliary finalisation rows. This contract declares the
    deterministic ordering plus the evidence lineage so the frontend renders
    workflow-first rather than synthesising its own order, and so a precedence
    violation can be detected and counted (JVNAUTOSCI-2381).
    """

    selected_workflow_evidence_present = bool(selected_workflow_id) or bool(
        evidence_sources
    )

    precedence: list[str] = []
    if selected_workflow_evidence_present:
        precedence.append("selected_workflow_execution")
    elif has_tool_evidence:
        precedence.append("tool_execution")
    if blocker_summary:
        precedence.append("blocker_next_action")
    precedence.append("finalisation")
    precedence.append("auxiliary")

    return {
        "schema_version": "thinking_progress_contract.v1",
        "precedence_rule": "selected_workflow_execution_before_finalisation",
        "default_row_precedence": precedence,
        "selected_workflow_evidence_present": selected_workflow_evidence_present,
        "selected_workflow_evidence_sources": list(evidence_sources),
        "blocker_present": bool(blocker_summary),
        "finalisation_demoted": bool(
            post_processing_only and selected_workflow_evidence_present
        ),
    }


def _copy_progress_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _extract_prompt_context_diagnostics(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    diagnostics = _copy_progress_mapping(value.get("prompt_context_diagnostics"))
    if diagnostics:
        return diagnostics
    outputs = value.get("outputs")
    if isinstance(outputs, Mapping):
        diagnostics = _copy_progress_mapping(outputs.get("prompt_context_diagnostics"))
        if diagnostics:
            return diagnostics
    envelope = value.get("llm_step_envelope")
    if isinstance(envelope, Mapping):
        diagnostics = _copy_progress_mapping(envelope.get("prompt_context_diagnostics"))
        if diagnostics:
            return diagnostics
    return None


def _extract_prompt_char_count(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None

    direct_candidates = (
        value.get("prompt_char_count"),
        value.get("rendered_prompt_chars"),
    )
    for candidate in direct_candidates:
        numeric = _progress_number(candidate)
        if numeric is not None:
            return int(max(0.0, numeric))

    prompt_context_diagnostics = _extract_prompt_context_diagnostics(value)
    if isinstance(prompt_context_diagnostics, Mapping):
        numeric = _progress_number(
            prompt_context_diagnostics.get("rendered_prompt_chars")
        )
        if numeric is not None:
            return int(max(0.0, numeric))

    for key in (
        "prompt",
        "prompt_preview",
        "llm_prompt_preview",
        "input",
        "input_preview",
    ):
        capture = value.get(key)
        if isinstance(capture, Mapping):
            numeric = _progress_number(capture.get("char_count")) or _progress_number(
                capture.get("content_char_count")
            )
            if numeric is not None:
                return int(max(0.0, numeric))
            text = _progress_str(capture.get("text")) or _progress_str(
                capture.get("preview")
            )
            if text:
                return len(text)
        elif isinstance(capture, str) and capture:
            return len(capture)

    llm_request = value.get("llm_request")
    if isinstance(llm_request, Mapping):
        prompt_count = _extract_prompt_char_count(llm_request)
        if prompt_count is not None:
            return prompt_count

    return None


def _llm_call_alert_key(alert: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _progress_str(alert.get("call_id")),
        _progress_str(alert.get("stage")),
        _progress_str(alert.get("model")),
        _progress_number(alert.get("prompt_char_count")),
    )


def _build_large_llm_call_alerts(
    payload: Mapping[str, Any],
    *,
    diagnostic_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sources: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        sources.append(payload)

    llm_calls = payload.get("llm_calls")
    if isinstance(llm_calls, list):
        sources.extend(entry for entry in llm_calls if isinstance(entry, Mapping))

    sources.extend(entry for entry in diagnostic_events if isinstance(entry, Mapping))

    alerts: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for source in sources:
        prompt_char_count = _extract_prompt_char_count(source)
        if (
            prompt_char_count is None
            or prompt_char_count < _THINKING_LARGE_LLM_PROMPT_CHAR_THRESHOLD
        ):
            continue
        prompt_context_diagnostics = _extract_prompt_context_diagnostics(source)
        prompt_context_stage = (
            _canonicalise_turn_execution_stage_id(
                prompt_context_diagnostics.get("workflow_state_id")
            )
            if isinstance(prompt_context_diagnostics, Mapping)
            else None
        )
        stage = (
            prompt_context_stage
            or _canonicalise_turn_execution_stage_id(source.get("workflow_stage_id"))
            or _canonicalise_turn_execution_stage_id(source.get("stage"))
            or _canonicalise_turn_execution_stage_id(source.get("phase"))
            or "unscoped"
        )
        alert = {
            "schema_version": "thinking_large_llm_call_alert.v1",
            "stage": stage,
            "stage_label": _progress_str(source.get("stage_label"))
            or _progress_str(source.get("phase_label"))
            or _default_stage_label(stage),
            "model": _progress_str(source.get("model"))
            or _progress_str(source.get("model_name")),
            "provider": _progress_str(source.get("provider")),
            "duration_ms": (
                int(max(0.0, _progress_number(source.get("duration_ms")) or 0.0))
                if _progress_number(source.get("duration_ms")) is not None
                else None
            ),
            "prompt_char_count": prompt_char_count,
            "threshold_prompt_chars": _THINKING_LARGE_LLM_PROMPT_CHAR_THRESHOLD,
            "reason": "prompt_chars_exceeded_threshold",
        }
        call_id = _progress_str(source.get("call_id")) or _progress_str(
            source.get("llm_call_id")
        )
        if call_id:
            alert["call_id"] = call_id
        if isinstance(prompt_context_diagnostics, Mapping):
            alert["prompt_context_diagnostics"] = dict(prompt_context_diagnostics)
        key = _llm_call_alert_key(alert)
        if key in seen:
            continue
        seen.add(key)
        alerts.append({key: value for key, value in alert.items() if value is not None})
    return alerts[-5:]


def _latest_failure_event_summary(
    diagnostic_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    failure_statuses = {
        "error",
        "failed",
        "failure",
        "tool_failed",
        "tool_blocked",
    }
    for entry in reversed(diagnostic_events):
        status = (_progress_str(entry.get("status")) or "").lower()
        success = entry.get("success")
        has_failure = (
            status in failure_statuses
            or success is False
            or bool(_progress_str(entry.get("error")))
            or bool(_progress_str(entry.get("error_code")))
            or bool(_progress_str(entry.get("failure_kind")))
        )
        if not has_failure:
            continue
        stage = (
            _canonicalise_turn_execution_stage_id(entry.get("workflow_stage_id"))
            or _canonicalise_turn_execution_stage_id(entry.get("stage"))
            or _canonicalise_turn_execution_stage_id(entry.get("phase"))
        )
        summary = {
            "schema_version": "thinking_recovery_failure_summary.v1",
            "status": _progress_str(entry.get("status")),
            "stage": stage,
            "stage_label": _progress_str(entry.get("stage_label"))
            or _progress_str(entry.get("phase_label"))
            or (_default_stage_label(stage) if stage else None),
            "tool": _progress_str(entry.get("tool")),
            "workflow_task": _progress_str(entry.get("workflow_task")),
            "error_code": _progress_str(entry.get("error_code")),
            "error_class": _progress_str(entry.get("error_class")),
            "failure_kind": _progress_str(entry.get("failure_kind")),
            "error": _progress_str(entry.get("error")),
        }
        return {key: value for key, value in summary.items() if value is not None}
    return None


def _extract_recovery_attempt_count(
    payload: Mapping[str, Any],
    *,
    diagnostic_events: list[dict[str, Any]],
) -> int | None:
    candidate_keys = (
        "completion_gate_loop_attempts",
        "recovery_attempt_count",
        "recovery_attempt",
        "recovery_attempt_no",
        "fallback_attempt_no",
    )
    for source in (payload, *reversed(diagnostic_events)):
        if not isinstance(source, Mapping):
            continue
        for key in candidate_keys:
            numeric = _progress_number(source.get(key))
            if numeric is not None and numeric > 0:
                return int(numeric)
    return None


def _latest_recovery_stage_from_progress(
    payload: Mapping[str, Any],
    *,
    diagnostic_events: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    current_stage = (
        _canonicalise_recovery_stage(payload.get("phase"))
        or _canonicalise_recovery_stage(payload.get("stage"))
        or _canonicalise_recovery_stage(payload.get("workflow_stage_id"))
    )
    if current_stage:
        return current_stage, _default_stage_label(current_stage)

    workflow_stage_path = payload.get("workflow_stage_path")
    if isinstance(workflow_stage_path, Mapping):
        path = workflow_stage_path.get("path")
        if isinstance(path, list):
            for entry in reversed(path):
                if not isinstance(entry, Mapping):
                    continue
                stage_id = _canonicalise_recovery_stage(entry.get("stage_id"))
                if not stage_id:
                    continue
                return (
                    stage_id,
                    _progress_str(entry.get("stage_label"))
                    or _default_stage_label(stage_id),
                )

    phase_history = payload.get("phase_history")
    if isinstance(phase_history, list):
        for entry in reversed(phase_history):
            if not isinstance(entry, Mapping):
                continue
            stage_id = _canonicalise_recovery_stage(entry.get("phase"))
            if not stage_id:
                continue
            return (
                stage_id,
                _progress_str(entry.get("phaseLabel"))
                or _default_stage_label(stage_id),
            )

    for entry in reversed(diagnostic_events):
        stage_id = (
            _canonicalise_recovery_stage(entry.get("workflow_stage_id"))
            or _canonicalise_recovery_stage(entry.get("stage"))
            or _canonicalise_recovery_stage(entry.get("phase"))
        )
        if not stage_id:
            continue
        return (
            stage_id,
            _progress_str(entry.get("stage_label"))
            or _progress_str(entry.get("phase_label"))
            or _default_stage_label(stage_id),
        )

    prompt_context_diagnostics = _extract_prompt_context_diagnostics(payload)
    if isinstance(prompt_context_diagnostics, Mapping):
        stage_id = _canonicalise_recovery_stage(
            prompt_context_diagnostics.get("workflow_state_id")
        )
        if stage_id:
            return stage_id, _default_stage_label(stage_id)

    return None, None


def _build_thinking_recovery_progress_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    diagnostic_events = [
        cast(dict[str, Any], entry)
        for entry in payload.get("diagnostic_events", [])
        if isinstance(entry, dict)
    ]
    latest_recovery_stage, latest_recovery_stage_label = (
        _latest_recovery_stage_from_progress(
            payload,
            diagnostic_events=diagnostic_events,
        )
    )
    prompt_context_diagnostics = _extract_prompt_context_diagnostics(payload)
    recovery_prompt_context = (
        dict(prompt_context_diagnostics)
        if isinstance(prompt_context_diagnostics, Mapping)
        and isinstance(
            prompt_context_diagnostics.get("recovery_context_compaction"),
            Mapping,
        )
        else None
    )

    if not latest_recovery_stage and not recovery_prompt_context:
        return None

    current_stage = (
        _canonicalise_turn_execution_stage_id(payload.get("phase"))
        or _canonicalise_turn_execution_stage_id(payload.get("stage"))
        or _canonicalise_turn_execution_stage_id(payload.get("workflow_stage_id"))
    )
    active = _is_recovery_stage(current_stage)
    finalising_after_recovery = bool(
        current_stage == "response_finalising" and latest_recovery_stage
    )
    latest_failure = _latest_failure_event_summary(diagnostic_events)
    large_llm_call_alerts = _build_large_llm_call_alerts(
        payload,
        diagnostic_events=diagnostic_events,
    )
    attempt_count = _extract_recovery_attempt_count(
        payload,
        diagnostic_events=diagnostic_events,
    )

    recovery_payload: dict[str, Any] = {
        "schema_version": "thinking_recovery_progress.v1",
        "active": active,
        "recent": bool(latest_recovery_stage),
        "finalising_after_recovery": finalising_after_recovery,
        "current_stage_id": current_stage,
        "latest_recovery_stage_id": latest_recovery_stage,
        "latest_recovery_stage_label": latest_recovery_stage_label
        or (
            _default_stage_label(latest_recovery_stage)
            if latest_recovery_stage
            else None
        ),
        "attempt_count": attempt_count,
        "latest_failure": latest_failure,
        "prompt_context_diagnostics": recovery_prompt_context,
        "large_llm_call_alerts": large_llm_call_alerts,
    }
    return {
        key: value
        for key, value in recovery_payload.items()
        if value is not None and value != []
    }


def _build_thinking_interpretability_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    stage = _progress_str(payload.get("stage")) or _progress_str(payload.get("phase"))
    stage_label = _progress_str(payload.get("stage_label")) or _progress_str(
        payload.get("phase_label")
    )
    stage_label = stage_label or (_default_stage_label(stage) if stage else None)
    subtask = _progress_str(payload.get("subtask")) or _progress_str(
        payload.get("workflow_task")
    )
    result_summary = _progress_str(payload.get("result_summary"))

    selected_workflow_id, selected_workflow_name = (
        _extract_selected_workflow_identity_from_progress(payload)
    )

    tool_history = payload.get("tool_history")
    tool_names: list[str] = []
    if isinstance(tool_history, list):
        for entry in tool_history:
            if not isinstance(entry, Mapping):
                continue
            tool_name = _progress_str(entry.get("tool")) or _progress_str(
                entry.get("workflow_task")
            )
            if tool_name and tool_name not in tool_names:
                tool_names.append(tool_name)

    tool_count = _coerce_non_negative_int(payload.get("tool_call_count"))
    has_tool_evidence = (
        bool(tool_names) or tool_count > 0 or bool(stage and stage.startswith("tool_"))
    )

    if selected_workflow_id:
        execution_family = "selected_workflow"
        progress_kind = "workflow_execution"
        identity_summary = (
            f"Selected workflow: {selected_workflow_name} ({selected_workflow_id})"
            if selected_workflow_name
            else f"Selected workflow: {selected_workflow_id}"
        )
    elif has_tool_evidence:
        execution_family = "tool_orchestration"
        progress_kind = "tool_execution"
        if tool_names:
            preview = ", ".join(tool_names[:3])
            suffix = f" +{len(tool_names) - 3} more" if len(tool_names) > 3 else ""
            identity_summary = f"General tool use: {preview}{suffix}"
        else:
            identity_summary = "General tool use"
    else:
        execution_family = "chat_response"
        progress_kind = "chat_generation"
        identity_summary = "Direct chat response"

    latest_core_stage = _latest_non_finalising_stage_from_path(payload)
    latest_core_stage_label = (
        _default_stage_label(latest_core_stage) if latest_core_stage else None
    )
    recovery_progress = payload.get("recovery_progress")
    recovery_progress = (
        dict(recovery_progress) if isinstance(recovery_progress, Mapping) else None
    )
    recovery_stage_label = (
        _progress_str(recovery_progress.get("latest_recovery_stage_label"))
        if isinstance(recovery_progress, Mapping)
        else None
    )
    if recovery_progress and stage == "response_finalising" and recovery_stage_label:
        progress_kind = "post_recovery_finalising"
        step_summary = f"Finalising response after {recovery_stage_label}."
        if result_summary:
            step_summary = f"{step_summary} {result_summary}"
    elif stage == "response_finalising" and latest_core_stage_label:
        progress_kind = "post_processing"
        step_summary = f"Finalising response after {latest_core_stage_label}."
        if result_summary:
            step_summary = f"{step_summary} {result_summary}"
    elif recovery_progress and recovery_stage_label:
        progress_kind = "recovery"
        step_bits = [recovery_stage_label]
        attempt_count = _progress_number(recovery_progress.get("attempt_count"))
        if attempt_count is not None:
            step_bits.append(f"attempt {int(max(0.0, attempt_count))}")
        latest_failure = recovery_progress.get("latest_failure")
        if isinstance(latest_failure, Mapping):
            failure_code = _progress_str(latest_failure.get("error_code"))
            failure_kind = _progress_str(latest_failure.get("failure_kind"))
            failure_tool = _progress_str(latest_failure.get("tool")) or _progress_str(
                latest_failure.get("workflow_task")
            )
            failure_bits = [
                "trigger",
                failure_tool,
                failure_code or failure_kind,
            ]
            step_bits.append(" ".join(bit for bit in failure_bits if bit))
        if result_summary:
            step_bits.append(result_summary)
        step_summary = " · ".join(bit for bit in step_bits if bit)
    else:
        step_bits = [stage_label, subtask, result_summary]
        step_summary = " · ".join(bit for bit in step_bits if bit)

    blocker_summary = _progress_str(payload.get("error"))
    if not blocker_summary:
        blocker_summary = _progress_str(payload.get("pending_reason"))
    if not blocker_summary:
        blocker_summary = _progress_str(payload.get("failure_kind"))

    post_processing_only = bool(
        stage == "response_finalising" and latest_core_stage is not None
    )
    evidence_sources = _selected_workflow_evidence_sources(payload)
    progress_contract = _build_thinking_progress_precedence_contract(
        selected_workflow_id=selected_workflow_id,
        has_tool_evidence=has_tool_evidence,
        blocker_summary=blocker_summary or None,
        post_processing_only=post_processing_only,
        evidence_sources=evidence_sources,
    )

    return {
        "schema_version": "thinking_interpretability.v1",
        "execution_family": execution_family,
        "progress_kind": progress_kind,
        "identity_summary": identity_summary,
        "step_summary": step_summary or None,
        "blocker_summary": blocker_summary or None,
        "selected_workflow_id": selected_workflow_id,
        "selected_workflow_name": selected_workflow_name,
        "stage": stage,
        "stage_label": stage_label,
        "post_processing_only": post_processing_only,
        "progress_contract": progress_contract,
        "recovery_progress": recovery_progress,
    }


def _iso_utc_to_epoch_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000.0)


def _normalise_workflow_discovery_progress_payload(
    workflow_discovery: Mapping[str, Any] | None,
    *,
    query: str | None = None,
    namespace: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Return a stable workflow-discovery payload for live progress rendering.

    The thinking card needs a deterministic shape even when discovery produces
    zero matches. This keeps "no workflow found" distinct from "no payload was
    emitted yet", and gives the frontend one canonical contract to render.
    """

    payload = (
        {
            key: value
            for key, value in workflow_discovery.items()
            if isinstance(key, str)
        }
        if isinstance(workflow_discovery, Mapping)
        else {}
    )

    matches_raw = payload.get("matches")
    matches = (
        [dict(item) for item in matches_raw if isinstance(item, Mapping)]
        if isinstance(matches_raw, list)
        else []
    )
    payload["matches"] = matches

    candidates_raw = payload.get("candidates")
    if isinstance(candidates_raw, list):
        payload["candidates"] = [
            dict(item) for item in candidates_raw if isinstance(item, Mapping)
        ]
    else:
        payload["candidates"] = list(matches)

    routing_matches_raw = payload.get("routing_matches")
    if isinstance(routing_matches_raw, list):
        payload["routing_matches"] = [
            dict(item) for item in routing_matches_raw if isinstance(item, Mapping)
        ]
    else:
        payload["routing_matches"] = list(matches)

    payload["match_count"] = len(payload["matches"])
    payload["candidate_count"] = len(payload["candidates"])

    if isinstance(query, str) and query.strip():
        payload.setdefault("query", query.strip())
        payload.setdefault("requested_query", query.strip())

    if isinstance(namespace, str) and namespace.strip():
        payload["namespace"] = namespace.strip()

    errors: list[str] = []
    existing_errors = payload.get("errors")
    if isinstance(existing_errors, list):
        errors.extend(
            str(item).strip()
            for item in existing_errors
            if isinstance(item, str) and item.strip()
        )
    elif isinstance(existing_errors, str) and existing_errors.strip():
        errors.append(existing_errors.strip())

    if isinstance(error, str) and error.strip():
        errors.append(error.strip())

    payload["errors"] = errors or None
    return payload


def _canonicalise_recovery_stage(stage: Any) -> str | None:
    clean_stage = _progress_str(stage)
    if not clean_stage:
        return None
    normalised = clean_stage.replace("-", "_").replace(" ", "_").strip().lower()
    if normalised in _RECOVERY_STAGE_IDS:
        return normalised
    return _RECOVERY_STAGE_ALIASES.get(normalised)


def _is_recovery_stage(stage: Any) -> bool:
    return _canonicalise_recovery_stage(stage) is not None


def _canonicalise_live_runtime_stage(stage: Any) -> str | None:
    clean_stage = _progress_str(stage)
    if not clean_stage:
        return None
    recovery_stage = _canonicalise_recovery_stage(clean_stage)
    if recovery_stage:
        return recovery_stage
    if clean_stage == "workflow_discovery_complete":
        return "workflow_discovery"
    if clean_stage == "orchestrator_start":
        return "workflow_dispatch_prepare"
    if clean_stage == "orchestrator_end":
        return None
    return clean_stage


def _normalise_live_runtime_stage_sequence(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    runtime_stages: list[str] = []
    last_stage_normalised: str | None = None
    for item in value:
        canonical_stage = _canonicalise_live_runtime_stage(item)
        if not canonical_stage:
            continue
        canonical_stage_normalised = canonical_stage.strip().lower()
        if canonical_stage_normalised == last_stage_normalised:
            continue
        runtime_stages.append(canonical_stage)
        last_stage_normalised = canonical_stage_normalised
    return runtime_stages


def _append_live_runtime_stage(runtime_stages: list[str], stage: Any) -> list[str]:
    canonical_stage = _canonicalise_live_runtime_stage(stage)
    if not canonical_stage:
        return list(runtime_stages)

    normalised_candidate = canonical_stage.strip().lower()
    if runtime_stages:
        last_stage = _progress_str(runtime_stages[-1])
        if last_stage and last_stage.strip().lower() == normalised_candidate:
            return list(runtime_stages)

    return [*runtime_stages, canonical_stage]


def _normalise_live_stage_summary_map(
    value: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}

    summary_map: dict[str, dict[str, Any]] = {}
    for raw_stage_id, raw_summary in value.items():
        stage_id = _canonicalise_live_runtime_stage(raw_stage_id)
        if not stage_id or not isinstance(raw_summary, Mapping):
            continue
        summary_map[stage_id] = {
            key: item for key, item in raw_summary.items() if isinstance(key, str)
        }
    return summary_map


def _build_live_workflow_stage_path_from_runtime_stages(
    runtime_stages: list[str], workflow_id: str | None
) -> dict[str, Any]:
    return build_conversation_turn_stage_path(
        runtime_stages=runtime_stages,
        workflow_id=workflow_id,
        selected_workflow_id=workflow_id,
    )


def _build_live_workflow_stage_path(
    state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        return build_conversation_turn_stage_path(
            runtime_stages=(),
            workflow_id=None,
            selected_workflow_id=None,
        )

    runtime_stages = _normalise_live_runtime_stage_sequence(
        state.get("_workflow_runtime_stages")
    )

    if not runtime_stages:
        diagnostic_events_raw = state.get("diagnostic_events")
        diagnostic_events = (
            [
                cast(dict[str, Any], entry)
                for entry in diagnostic_events_raw
                if isinstance(entry, dict)
            ]
            if isinstance(diagnostic_events_raw, list)
            else []
        )
        runtime_stages = []
        for entry in diagnostic_events:
            runtime_stages = _append_live_runtime_stage(
                runtime_stages,
                _progress_str(entry.get("phase")) or _progress_str(entry.get("stage")),
            )

    current_stage = _canonicalise_live_runtime_stage(
        _progress_str(state.get("phase")) or _progress_str(state.get("stage"))
    )
    if current_stage:
        runtime_stages = _append_live_runtime_stage(runtime_stages, current_stage)
    selected_workflow_id = _progress_str(state.get("selected_workflow_id"))
    return _build_live_workflow_stage_path_from_runtime_stages(
        runtime_stages,
        selected_workflow_id,
    )


def _normalise_progress_events_from_diagnostic_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    progress_events: list[dict[str, Any]] = []
    for entry in events:
        stage = _progress_str(entry.get("stage")) or _progress_str(entry.get("phase"))
        subtask = (
            _progress_str(entry.get("subtask"))
            or _progress_str(entry.get("tool"))
            or _progress_str(entry.get("workflow_task"))
        )

        sequence_no_raw = _progress_number(entry.get("sequence_no"))
        sequence_no = int(sequence_no_raw) if sequence_no_raw is not None else None

        idle_raw = _progress_number(entry.get("idle_ms"))
        if idle_raw is None:
            idle_raw = _progress_number(entry.get("activity_idle_ms"))
        idle_ms = int(idle_raw) if idle_raw is not None else None
        duration_raw = _progress_number(entry.get("duration_ms"))
        duration_ms = int(max(0.0, duration_raw)) if duration_raw is not None else None
        model = _progress_str(entry.get("model"))
        success = entry.get("success")
        if not isinstance(success, bool):
            success = None

        progress_events.append(
            {
                "at_utc": _progress_str(entry.get("at_utc")),
                "status": _progress_str(entry.get("status")),
                "stage": stage,
                "sequence_no": sequence_no,
                "goal_label": _progress_str(entry.get("goal_label")),
                "liveness_state": _progress_str(entry.get("liveness_state")),
                "idle_ms": idle_ms,
                "subtask": subtask,
                "duration_ms": duration_ms,
                "model": model,
                "success": success,
            }
        )
    return progress_events


def _derive_phase_history_from_diagnostic_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    phase_history: list[dict[str, Any]] = []
    last_phase: str | None = None

    for entry in events:
        status = (_progress_str(entry.get("status")) or "").lower()
        if status != "phase_transition":
            continue

        phase = _progress_str(entry.get("phase")) or _progress_str(entry.get("stage"))
        if not phase:
            continue
        if last_phase == phase:
            continue

        phase_history.append(
            {
                "phase": phase,
                "phaseLabel": _progress_str(entry.get("phase_label"))
                or _progress_str(entry.get("stage_label")),
                "timestamp": _iso_utc_to_epoch_ms(entry.get("at_utc")),
            }
        )
        last_phase = phase

    return phase_history[-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT:]


def _normalise_phase_history_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    normalised: list[dict[str, Any]] = []
    for raw_entry in value:
        if not isinstance(raw_entry, Mapping):
            continue

        phase = _progress_str(raw_entry.get("phase")) or _progress_str(
            raw_entry.get("stage")
        )
        if not phase:
            continue

        timestamp = _progress_number(raw_entry.get("timestamp"))
        at_utc = _progress_str(raw_entry.get("at_utc"))
        if timestamp is None and at_utc:
            timestamp = _iso_utc_to_epoch_ms(at_utc)
        if timestamp is None:
            continue

        stage_id = _canonicalise_live_runtime_stage(phase) or phase
        phase_label = (
            _progress_str(raw_entry.get("phaseLabel"))
            or _progress_str(raw_entry.get("phase_label"))
            or _progress_str(raw_entry.get("stage_label"))
            or _default_stage_label(stage_id)
        )
        normalised.append(
            {
                "phase": phase,
                "phaseLabel": phase_label,
                "timestamp": int(max(0.0, timestamp)),
                "at_utc": at_utc,
                "sequence_no": int(
                    max(0.0, _progress_number(raw_entry.get("sequence_no")) or 0)
                )
                or None,
            }
        )

    return normalised[-_TOOL_PROGRESS_PHASE_HISTORY_LIMIT:]


def _append_preserved_phase_history_entry(
    entries: list[dict[str, Any]],
    *,
    phase: str | None,
    phase_label: str | None,
    timestamp_ms: int,
    at_utc: str | None,
    sequence_no: int | None,
) -> list[dict[str, Any]]:
    clean_phase = _progress_str(phase)
    if not clean_phase:
        return entries

    updated = [dict(entry) for entry in entries if isinstance(entry, Mapping)]
    clean_stage_id = _canonicalise_live_runtime_stage(clean_phase) or clean_phase
    clean_phase_label = _progress_str(phase_label) or _default_stage_label(
        clean_stage_id
    )
    clean_timestamp = int(max(0, timestamp_ms))
    clean_sequence_no = (
        int(max(0, sequence_no)) if isinstance(sequence_no, int) else None
    )

    if updated:
        last_entry = updated[-1]
        last_phase = _progress_str(last_entry.get("phase"))
        if last_phase == clean_phase:
            if not _progress_str(last_entry.get("phaseLabel")):
                last_entry["phaseLabel"] = clean_phase_label
            if not _progress_str(last_entry.get("at_utc")) and at_utc:
                last_entry["at_utc"] = at_utc
            if _progress_number(last_entry.get("timestamp")) is None:
                last_entry["timestamp"] = clean_timestamp
            if (
                clean_sequence_no is not None
                and _progress_number(last_entry.get("sequence_no")) is None
            ):
                last_entry["sequence_no"] = clean_sequence_no
            return updated[-_TOOL_PROGRESS_PHASE_HISTORY_LIMIT:]

    updated.append(
        {
            "phase": clean_phase,
            "phaseLabel": clean_phase_label,
            "timestamp": clean_timestamp,
            "at_utc": at_utc,
            "sequence_no": clean_sequence_no,
        }
    )
    return updated[-_TOOL_PROGRESS_PHASE_HISTORY_LIMIT:]


def _extract_phase_history_from_progress_state(
    progress_state: Mapping[str, Any] | None,
    *,
    diagnostic_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if isinstance(progress_state, Mapping):
        preserved_entries = _normalise_phase_history_entries(
            progress_state.get("_phase_history")
        )
        if preserved_entries:
            return preserved_entries

        payload_entries = _normalise_phase_history_entries(
            progress_state.get("phase_history")
        )
        if payload_entries:
            return payload_entries

    return _derive_phase_history_from_diagnostic_events(diagnostic_events or [])


def _build_progress_events_from_phase_history(
    phase_history: list[dict[str, Any]],
    *,
    latest_progress: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not phase_history:
        return []

    latest_payload = latest_progress if isinstance(latest_progress, Mapping) else {}
    latest_stage = _canonicalise_live_runtime_stage(
        _progress_str(latest_payload.get("phase"))
        or _progress_str(latest_payload.get("stage"))
    )
    latest_status = _progress_str(latest_payload.get("status"))
    latest_liveness_state = _progress_str(latest_payload.get("liveness_state"))
    latest_idle_ms = _progress_number(latest_payload.get("idle_ms"))
    latest_subtask = (
        _progress_str(latest_payload.get("subtask"))
        or _progress_str(latest_payload.get("tool"))
        or _progress_str(latest_payload.get("workflow_task"))
    )
    goal_label = _normalise_progress_goal_label(latest_payload.get("goal_label"))

    progress_events: list[dict[str, Any]] = []
    for index, entry in enumerate(phase_history):
        phase = _progress_str(entry.get("phase"))
        if not phase:
            continue
        stage_id = _canonicalise_live_runtime_stage(phase) or phase
        is_latest = index == len(phase_history) - 1
        progress_events.append(
            {
                "at_utc": _progress_str(entry.get("at_utc")),
                "status": (
                    latest_status if is_latest and latest_status else "phase_transition"
                ),
                "stage": stage_id,
                "sequence_no": _progress_number(entry.get("sequence_no")),
                "liveness_state": (
                    latest_liveness_state
                    if is_latest and latest_liveness_state
                    else "active"
                ),
                "idle_ms": (
                    int(max(0.0, latest_idle_ms))
                    if is_latest and latest_idle_ms is not None
                    else 0
                ),
                "subtask": latest_subtask if is_latest and latest_subtask else None,
                "goal_label": goal_label,
            }
        )

    if latest_stage and progress_events:
        progress_events[-1]["stage"] = latest_stage

    return progress_events[-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT:]


def _build_activity_history_from_phase_history(
    phase_history: list[dict[str, Any]],
    *,
    stage_diagnostics: list[dict[str, Any]],
    latest_progress: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not phase_history:
        return []

    latest_payload = latest_progress if isinstance(latest_progress, Mapping) else {}
    latest_stage = _canonicalise_live_runtime_stage(
        _progress_str(latest_payload.get("phase"))
        or _progress_str(latest_payload.get("stage"))
    )
    latest_status = (_progress_str(latest_payload.get("status")) or "").lower()
    latest_terminal_state = _resolve_latest_turn_execution_activity_state(
        latest_payload
    )
    stage_diagnostic_map = {
        _canonicalise_live_runtime_stage(entry.get("stage_id"))
        or _progress_str(entry.get("stage_id")): entry
        for entry in stage_diagnostics
        if isinstance(entry, Mapping)
    }

    activity_history: list[dict[str, Any]] = []
    for index, entry in enumerate(phase_history):
        phase = _progress_str(entry.get("phase"))
        if not phase:
            continue
        stage_id = _canonicalise_live_runtime_stage(phase) or phase
        stage_diagnostic = stage_diagnostic_map.get(stage_id) or {}
        is_latest = index == len(phase_history) - 1
        state = "success"
        if is_latest:
            if latest_terminal_state in {"success", "failure"}:
                state = latest_terminal_state
            elif latest_status in {"error", "failed", "cancelled"}:
                state = "failure"
            else:
                state = "pending"

        activity_history.append(
            {
                "sequenceNo": _progress_number(entry.get("sequence_no")),
                "atUtc": _progress_str(entry.get("at_utc")),
                "stage": stage_id,
                "status": _progress_str(stage_diagnostic.get("latest_status"))
                or (
                    "heartbeat"
                    if is_latest and latest_status == "heartbeat"
                    else "phase_transition"
                ),
                "eventKind": "phase_transition",
                "label": _progress_str(entry.get("phaseLabel"))
                or _progress_str(stage_diagnostic.get("stage_label"))
                or _default_stage_label(stage_id),
                "detail": _progress_str(stage_diagnostic.get("latest_result_summary"))
                or _progress_str(stage_diagnostic.get("latest_error")),
                "detailHtml": "",
                "state": state,
                "isLowLevel": False,
                "groupCount": 1,
                "subtask": (
                    (
                        _progress_str(latest_payload.get("subtask"))
                        or _progress_str(latest_payload.get("tool"))
                        or _progress_str(latest_payload.get("workflow_task"))
                    )
                    if is_latest
                    else None
                ),
                "model": (
                    _progress_str(latest_payload.get("model")) if is_latest else None
                ),
            }
        )

    if latest_stage and activity_history:
        activity_history[-1]["stage"] = latest_stage

    return activity_history[-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT:]


def _resolve_latest_turn_execution_activity_state(
    latest_progress: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(latest_progress, Mapping):
        return None

    workflow_routing_diagnostics = latest_progress.get("workflow_routing_diagnostics")
    if isinstance(workflow_routing_diagnostics, Mapping):
        dispatch = workflow_routing_diagnostics.get("dispatch")
        if isinstance(dispatch, Mapping):
            dispatch_terminal_status = (
                (_progress_str(dispatch.get("dispatch_terminal_status")) or "")
                .strip()
                .lower()
            )
            if dispatch_terminal_status in {"failed", "failure"}:
                return "failure"
            if dispatch_terminal_status in {
                "completed",
                "complete",
                "done",
                "success",
                "succeeded",
            }:
                return "success"

    result_summary = (
        _progress_str(latest_progress.get("result_summary")) or ""
    ).strip()
    if result_summary.lower().endswith(":failed"):
        return "failure"

    terminal_status_candidates = (
        latest_progress.get("status"),
        latest_progress.get("orchestrator_status"),
        latest_progress.get("terminal_status"),
        latest_progress.get("final_status"),
    )
    for value in terminal_status_candidates:
        status = (_progress_str(value) or "").strip().lower()
        if status in {
            "error",
            "failed",
            "failure",
            "cancelled",
            "canceled",
            "aborted",
            "terminated",
        }:
            return "failure"
        if status in {"completed", "complete", "done", "success", "succeeded"}:
            return "success"

    return None


def _derive_tool_history_from_diagnostic_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tool_observations = derive_tool_observations_from_diagnostic_events(
        events,
        limit=_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT,
    )
    tool_history = tool_observations.get("tool_history")
    return (
        [
            cast(dict[str, Any], entry)
            for entry in tool_history
            if isinstance(entry, dict)
        ]
        if isinstance(tool_history, list)
        else []
    )


def _extract_tool_observation_summary_from_progress(
    progress_state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(progress_state, Mapping):
        return None

    has_tool_history = isinstance(progress_state.get("tool_history"), list)
    has_tool_counts = any(
        key in progress_state
        for key in (
            "tool_call_count",
            "tool_success_count",
            "tool_failure_count",
            "tool_pending_count",
            "tool_call_start_count",
            "tool_call_end_count",
        )
    )
    if not has_tool_history and not has_tool_counts:
        return None

    return update_tool_observation_summary(
        progress_state,
        None,
        limit=_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT,
    )


def _extract_live_stage_diagnostic_map(
    progress_state: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(progress_state, Mapping):
        return {}

    raw_stage_diagnostics = progress_state.get("stage_diagnostics")
    if isinstance(raw_stage_diagnostics, list):
        stage_map: dict[str, dict[str, Any]] = {}
        for entry in raw_stage_diagnostics:
            if not isinstance(entry, Mapping):
                continue
            stage_id = _canonicalise_live_runtime_stage(entry.get("stage_id"))
            if not stage_id:
                continue
            stage_map[stage_id] = {
                key: value for key, value in entry.items() if isinstance(key, str)
            }
        if stage_map:
            return stage_map

    summary_map = _normalise_live_stage_summary_map(
        progress_state.get("_stage_summaries")
    )
    return {
        stage_id: {
            "stage_id": stage_id,
            "stage_label": _progress_str(summary.get("stage_label"))
            or _default_stage_label(stage_id),
            "event_count": int(_progress_number(summary.get("event_count")) or 0),
            "latest_status": _progress_str(summary.get("latest_status")),
            "latest_at_utc": _progress_str(summary.get("latest_at_utc")),
            "latest_result_summary": _progress_str(
                summary.get("latest_result_summary")
            ),
            "latest_error": _progress_str(summary.get("latest_error")),
            "latest_subtask": _progress_str(summary.get("latest_subtask")),
            "latest_workflow_task": _progress_str(summary.get("latest_workflow_task")),
            "latest_tool": _progress_str(summary.get("latest_tool")),
        }
        for stage_id, summary in summary_map.items()
    }


def _copy_live_progress_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _build_live_llm_exchange_summary(
    *,
    stage_id: str,
    live_stage_payload: Mapping[str, Any] | None,
    latest_stage_event: Mapping[str, Any] | None,
    latest_progress_payload: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    latest_progress_mapping: Mapping[str, Any] = (
        cast(Mapping[str, Any], latest_progress_payload)
        if isinstance(latest_progress_payload, Mapping)
        else {}
    )
    # JVNAUTOSCI-2133: Prefer workflow_stage_id (canonical workflow stage label
    # threaded by the chokepoint) over orchestrator-internal `stage`/`phase`
    # labels when matching live progress events to workflow stage rows.
    latest_workflow_stage_id = _progress_str(
        latest_progress_mapping.get("workflow_stage_id")
    )
    latest_stage_id = latest_workflow_stage_id or _canonicalise_live_runtime_stage(
        _progress_str(latest_progress_mapping.get("phase"))
        or _progress_str(latest_progress_mapping.get("stage"))
    )
    candidate_sources: list[Mapping[str, Any]] = []
    if stage_id == latest_stage_id and isinstance(latest_progress_payload, Mapping):
        candidate_sources.append(latest_progress_mapping)
    if isinstance(latest_stage_event, Mapping):
        candidate_sources.append(latest_stage_event)
    if isinstance(live_stage_payload, Mapping):
        candidate_sources.append(live_stage_payload)

    source: Mapping[str, Any] | None = None
    for candidate in candidate_sources:
        if (
            isinstance(candidate.get("llm_request"), Mapping)
            or isinstance(candidate.get("llm_response_preview"), Mapping)
            or _progress_str(candidate.get("llm_request_state"))
        ):
            source = candidate
            break
    if not isinstance(source, Mapping):
        return None

    request_mapping_raw = source.get("llm_request")
    request_mapping: Mapping[str, Any] = (
        cast(Mapping[str, Any], request_mapping_raw)
        if isinstance(request_mapping_raw, Mapping)
        else {}
    )
    prompt_capture = _copy_live_progress_mapping(request_mapping.get("prompt"))
    context_summary = _copy_live_progress_mapping(
        request_mapping.get("context_summary")
    )
    response_preview = _copy_live_progress_mapping(source.get("llm_response_preview"))

    has_input = bool(prompt_capture) or bool(request_mapping.get("context_messages"))
    has_output = bool(response_preview)
    request_state = _progress_str(source.get("llm_request_state"))
    if not has_input and not has_output and not request_state:
        return None

    summary: dict[str, Any] = {
        "entry_type": "live_llm_request",
        "stage": stage_id,
        "llm_input_recorded": has_input,
        "llm_output_recorded": has_output,
    }
    if prompt_capture:
        summary["prompt_preview"] = prompt_capture
    if context_summary:
        summary["context_summary"] = context_summary
    context_message_count = _progress_number(
        request_mapping.get("context_message_count")
    )
    if context_message_count is not None:
        summary["context_message_count"] = int(max(0.0, context_message_count))
    tool_count = _progress_number(request_mapping.get("tool_count"))
    if tool_count is not None:
        summary["tool_definition_count"] = int(max(0.0, tool_count))
    if response_preview:
        summary["response_preview"] = response_preview
    selected_model = _progress_str(source.get("model"))
    if selected_model:
        summary["selected_model"] = selected_model
    selected_provider = _progress_str(source.get("provider"))
    if selected_provider:
        summary["selected_provider"] = selected_provider
    fallback_attempt_no = _progress_number(source.get("fallback_attempt_no"))
    if fallback_attempt_no is not None:
        summary["fallback_attempt_no"] = int(max(0.0, fallback_attempt_no))
    fallback_candidate_count = _progress_number(source.get("fallback_candidate_count"))
    if fallback_candidate_count is not None:
        summary["fallback_candidate_count"] = int(max(0.0, fallback_candidate_count))
    if request_state:
        summary["llm_request_state"] = request_state
    prepared_at_utc = _progress_str(source.get("llm_request_prepared_at_utc"))
    if prepared_at_utc:
        summary["llm_request_prepared_at_utc"] = prepared_at_utc
    sent_at_utc = _progress_str(source.get("llm_request_sent_at_utc"))
    if sent_at_utc:
        summary["llm_request_sent_at_utc"] = sent_at_utc
    first_output_at_utc = _progress_str(source.get("llm_first_output_at_utc"))
    if first_output_at_utc:
        summary["llm_first_output_at_utc"] = first_output_at_utc

    return summary


def _workflow_candidate_name_matches_selected_id(
    candidate: Mapping[str, Any] | None,
    selected_workflow_id: str | None,
) -> str | None:
    if not isinstance(candidate, Mapping):
        return None

    candidate_workflow_id = (
        _progress_str(candidate.get("concept_id"))
        or _progress_str(candidate.get("workflow_id"))
        or _progress_str(candidate.get("id"))
    )
    if selected_workflow_id and candidate_workflow_id:
        if (
            candidate_workflow_id.strip().lower()
            != selected_workflow_id.strip().lower()
        ):
            return None

    return (
        _progress_str(candidate.get("name"))
        or _progress_str(candidate.get("workflow_name"))
        or _progress_str(candidate.get("label"))
    )


def _resolve_workflow_name_from_routing_diagnostics(
    selected_workflow_id: str | None,
    workflow_routing_diagnostics: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(workflow_routing_diagnostics, Mapping):
        return None

    selector_payload = workflow_routing_diagnostics.get("selector")
    if isinstance(selector_payload, Mapping):
        selected_candidate_name = _workflow_candidate_name_matches_selected_id(
            (
                selector_payload.get("selected_candidate")
                if isinstance(selector_payload.get("selected_candidate"), Mapping)
                else None
            ),
            selected_workflow_id,
        )
        if selected_candidate_name:
            return selected_candidate_name

        selector_candidates = selector_payload.get("candidate_entries")
        if isinstance(selector_candidates, list):
            for entry in selector_candidates:
                candidate_name = _workflow_candidate_name_matches_selected_id(
                    entry if isinstance(entry, Mapping) else None,
                    selected_workflow_id,
                )
                if candidate_name:
                    return candidate_name

    discovery_payload = workflow_routing_diagnostics.get("discovery")
    if not isinstance(discovery_payload, Mapping):
        return None

    for collection_name in ("routing_matches", "candidates", "excluded_candidates"):
        collection = discovery_payload.get(collection_name)
        if not isinstance(collection, list):
            continue
        for entry in collection:
            candidate_name = _workflow_candidate_name_matches_selected_id(
                entry if isinstance(entry, Mapping) else None,
                selected_workflow_id,
            )
            if candidate_name:
                return candidate_name

    return None


def _resolve_workflow_selection_fields(
    *,
    latest_progress_payload: Mapping[str, Any] | None,
    live_stage_payload: Mapping[str, Any] | None = None,
    latest_stage_event: Mapping[str, Any] | None = None,
    workflow_routing_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    latest_payload = (
        latest_progress_payload if isinstance(latest_progress_payload, Mapping) else {}
    )
    stage_payload = (
        live_stage_payload if isinstance(live_stage_payload, Mapping) else {}
    )
    stage_event = latest_stage_event if isinstance(latest_stage_event, Mapping) else {}
    routing_payload = (
        workflow_routing_diagnostics
        if isinstance(workflow_routing_diagnostics, Mapping)
        else {}
    )

    selected_workflow_id = (
        _progress_str(stage_payload.get("selected_workflow_id"))
        or _progress_str(stage_event.get("selected_workflow_id"))
        or _progress_str(latest_payload.get("selected_workflow_id"))
        or _progress_str(routing_payload.get("selected_workflow_id"))
    )
    selected_workflow_name = (
        _progress_str(stage_payload.get("selected_workflow_name"))
        or _progress_str(stage_event.get("selected_workflow_name"))
        or _progress_str(latest_payload.get("selected_workflow_name"))
        or _resolve_workflow_name_from_routing_diagnostics(
            selected_workflow_id,
            routing_payload,
        )
    )
    workflow_selector_verdict = (
        _progress_str(stage_payload.get("workflow_selector_verdict"))
        or _progress_str(stage_event.get("workflow_selector_verdict"))
        or _progress_str(latest_payload.get("workflow_selector_verdict"))
        or _progress_str(routing_payload.get("selector_verdict"))
    )
    workflow_selector_source = (
        _progress_str(stage_payload.get("workflow_selector_source"))
        or _progress_str(stage_event.get("workflow_selector_source"))
        or _progress_str(latest_payload.get("workflow_selector_source"))
        or _progress_str(routing_payload.get("selector_source"))
    )
    workflow_selection_rationale = (
        _progress_str(stage_payload.get("workflow_selection_rationale"))
        or _progress_str(stage_event.get("workflow_selection_rationale"))
        or _progress_str(latest_payload.get("workflow_selection_rationale"))
        or _progress_str(routing_payload.get("selection_rationale"))
    )

    resolved: dict[str, str] = {}
    if selected_workflow_id:
        resolved["selected_workflow_id"] = selected_workflow_id
    if selected_workflow_name:
        resolved["selected_workflow_name"] = selected_workflow_name
    if workflow_selector_verdict:
        resolved["workflow_selector_verdict"] = workflow_selector_verdict
    if workflow_selector_source:
        resolved["workflow_selector_source"] = workflow_selector_source
    if workflow_selection_rationale:
        resolved["workflow_selection_rationale"] = workflow_selection_rationale
    return resolved


def _extract_workflow_stage_path_from_progress(
    progress_state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(progress_state, Mapping):
        return None

    raw_stage_path = progress_state.get("workflow_stage_path")
    if not isinstance(raw_stage_path, Mapping):
        return None

    raw_path = raw_stage_path.get("path")
    if not isinstance(raw_path, list):
        return None

    path = [dict(entry) for entry in raw_path if isinstance(entry, Mapping)]
    if not path:
        return None

    stage_path = dict(raw_stage_path)
    stage_path["path"] = path
    return stage_path


def _latest_event_timestamp_ms(events: list[dict[str, Any]]) -> int | None:
    for entry in reversed(events):
        timestamp = _iso_utc_to_epoch_ms(entry.get("at_utc"))
        if timestamp is not None:
            return timestamp
    return None


def _normalise_llm_stage_calls(
    *,
    llm_calls: list[dict[str, Any]],
    diagnostic_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if llm_calls:
        source_entries: list[dict[str, Any]] = llm_calls
    else:
        source_entries = []
        for event in diagnostic_events:
            status = (_progress_str(event.get("status")) or "").lower()
            if status != "llm_call_end":
                continue
            source_entries.append(event)

    normalised: list[dict[str, Any]] = []

    def _duration_baseline(entry: Mapping[str, Any]) -> dict[str, Any]:
        baseline: dict[str, Any] = {}
        count = _progress_number(entry.get("historical_observation_count"))
        if count is None:
            return baseline
        baseline["historical_observation_count"] = int(max(0.0, count))
        for source_key in (
            "historical_mean_duration_ms",
            "historical_stddev_duration_ms",
            "historical_min_duration_ms",
            "historical_max_duration_ms",
            "duration_deviation_from_mean_ms",
            "duration_deviation_ratio",
            "duration_deviation_stddevs",
        ):
            value = _progress_number(entry.get(source_key))
            if value is not None:
                baseline[source_key] = value
        for source_key in (
            "historical_last_observed_at_utc",
            "duration_deviation_classification",
        ):
            value = _progress_str(entry.get(source_key))
            if value:
                baseline[source_key] = value
        return baseline

    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        duration_raw = _progress_number(entry.get("duration_ms"))
        if duration_raw is None:
            continue
        duration_ms = int(max(0.0, float(duration_raw)))
        stage = (
            _progress_str(entry.get("workflow_stage_id"))
            or _progress_str(entry.get("stage"))
            or _progress_str(entry.get("phase"))
            or "unscoped"
        )
        model = (
            _progress_str(entry.get("model"))
            or _progress_str(entry.get("model_name"))
            or "unknown"
        )
        row = {
            "stage": stage,
            "model": model,
            "provider": _progress_str(entry.get("provider")),
            "duration_ms": duration_ms,
        }
        prompt_char_count = _extract_prompt_char_count(entry)
        if prompt_char_count is not None:
            row["prompt_char_count"] = prompt_char_count
        row.update(_duration_baseline(entry))
        normalised.append(row)
    return normalised


def _build_timing_breakdown(
    *,
    diagnostic_events: list[dict[str, Any]],
    phase_history: list[dict[str, Any]],
    llm_calls: list[dict[str, Any]],
    elapsed_ms_value: int | None,
) -> dict[str, Any]:
    stage_totals: dict[str, dict[str, Any]] = {}
    stage_order: dict[str, int] = {}
    order_counter = 0

    def _touch_stage(stage: str) -> dict[str, Any]:
        nonlocal order_counter
        row = stage_totals.get(stage)
        if row is None:
            row = {
                "stage": stage,
                "elapsed_ms": None,
                "llm_elapsed_ms": 0,
                "llm_call_count": 0,
                "non_llm_elapsed_ms": None,
            }
            stage_totals[stage] = row
        if stage not in stage_order:
            stage_order[stage] = order_counter
            order_counter += 1
        return row

    latest_timestamp = _latest_event_timestamp_ms(diagnostic_events)
    first_phase_timestamp: int | None = None

    for index, phase_entry in enumerate(phase_history):
        stage = _progress_str(phase_entry.get("phase"))
        start_raw = _progress_number(phase_entry.get("timestamp"))
        if not stage or start_raw is None:
            continue
        start_ts = int(start_raw)
        if first_phase_timestamp is None:
            first_phase_timestamp = start_ts

        next_ts: int | None = None
        if index + 1 < len(phase_history):
            next_raw = _progress_number(phase_history[index + 1].get("timestamp"))
            if next_raw is not None:
                next_ts = int(next_raw)
        if next_ts is None:
            next_ts = latest_timestamp
        if next_ts is None:
            continue

        duration_ms = int(max(0, next_ts - start_ts))
        stage_row = _touch_stage(stage)
        existing_elapsed = stage_row.get("elapsed_ms")
        if isinstance(existing_elapsed, int):
            stage_row["elapsed_ms"] = existing_elapsed + duration_ms
        else:
            stage_row["elapsed_ms"] = duration_ms

    llm_stage_calls = _normalise_llm_stage_calls(
        llm_calls=llm_calls,
        diagnostic_events=diagnostic_events,
    )
    llm_by_stage_model: dict[tuple[str, str], dict[str, Any]] = {}
    llm_total_ms = 0

    def _merge_duration_baseline(
        bucket: dict[str, Any], entry: Mapping[str, Any]
    ) -> None:
        count = entry.get("historical_observation_count")
        if not isinstance(count, int) or count <= 0:
            return
        existing_count = bucket.get("historical_observation_count")
        if isinstance(existing_count, int) and existing_count >= count:
            return
        for key in (
            "historical_observation_count",
            "historical_mean_duration_ms",
            "historical_stddev_duration_ms",
            "historical_min_duration_ms",
            "historical_max_duration_ms",
            "historical_last_observed_at_utc",
            "duration_deviation_from_mean_ms",
            "duration_deviation_ratio",
            "duration_deviation_stddevs",
            "duration_deviation_classification",
        ):
            if key in entry:
                bucket[key] = entry.get(key)

    for entry in llm_stage_calls:
        stage = cast(str, entry["stage"])
        model = cast(str, entry["model"])
        duration_ms = int(entry["duration_ms"])
        provider = _progress_str(entry.get("provider"))

        stage_row = _touch_stage(stage)
        stage_row["llm_elapsed_ms"] = int(stage_row["llm_elapsed_ms"]) + duration_ms
        stage_row["llm_call_count"] = int(stage_row["llm_call_count"]) + 1

        key = (stage, model)
        bucket = llm_by_stage_model.get(key)
        if bucket is None:
            bucket = {
                "stage": stage,
                "model": model,
                "provider": provider,
                "call_count": 0,
                "duration_ms": 0,
            }
            llm_by_stage_model[key] = bucket
        _merge_duration_baseline(bucket, entry)
        bucket["call_count"] = int(bucket["call_count"]) + 1
        bucket["duration_ms"] = int(bucket["duration_ms"]) + duration_ms
        prompt_char_count = _progress_number(entry.get("prompt_char_count"))
        if prompt_char_count is not None:
            bucket["max_prompt_char_count"] = max(
                int(bucket.get("max_prompt_char_count") or 0),
                int(max(0.0, prompt_char_count)),
            )
        if bucket.get("provider") is None and provider is not None:
            bucket["provider"] = provider

        llm_total_ms += duration_ms

    for row in stage_totals.values():
        elapsed = row.get("elapsed_ms")
        llm_elapsed = int(row.get("llm_elapsed_ms") or 0)
        if isinstance(elapsed, int):
            row["non_llm_elapsed_ms"] = max(0, elapsed - llm_elapsed)

    stage_rows = sorted(
        stage_totals.values(),
        key=lambda item: (
            stage_order.get(cast(str, item.get("stage")), 1_000_000),
            cast(str, item.get("stage")),
        ),
    )

    llm_rows = sorted(
        llm_by_stage_model.values(),
        key=lambda item: (
            stage_order.get(cast(str, item.get("stage")), 1_000_000),
            -int(item.get("duration_ms") or 0),
            cast(str, item.get("model")),
        ),
    )

    phase_elapsed_total = sum(
        int(row["elapsed_ms"])
        for row in stage_rows
        if isinstance(row.get("elapsed_ms"), int)
    )
    observed_timeline_ms: int | None = None
    if (
        first_phase_timestamp is not None
        and latest_timestamp is not None
        and latest_timestamp >= first_phase_timestamp
    ):
        observed_timeline_ms = int(latest_timestamp - first_phase_timestamp)

    return {
        "schema_version": "conversation_turn_timing_breakdown.v1",
        "stages": stage_rows,
        "llm_calls_by_stage_model": llm_rows,
        "totals": {
            "elapsed_ms": elapsed_ms_value,
            "observed_timeline_ms": observed_timeline_ms,
            "phase_elapsed_ms": phase_elapsed_total,
            "llm_elapsed_ms": llm_total_ms,
            "llm_call_count": len(llm_stage_calls),
        },
    }


def _attach_turn_timing_trace_to_breakdown(
    timing_breakdown: Mapping[str, Any] | None,
    turn_timing_trace: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(timing_breakdown) if isinstance(timing_breakdown, Mapping) else {}
    if not isinstance(turn_timing_trace, Mapping):
        return payload
    payload["operation_totals"] = list(turn_timing_trace.get("operation_totals") or [])
    payload["slowest_spans"] = list(turn_timing_trace.get("slowest_spans") or [])
    payload["model_prompt_summary"] = list(
        turn_timing_trace.get("model_prompt_summary") or []
    )
    summary = turn_timing_trace.get("summary")
    if isinstance(summary, Mapping):
        totals = dict(payload.get("totals") or {})
        for key in (
            "operation_elapsed_ms",
            "llm_elapsed_ms",
            "tool_elapsed_ms",
            "phase_elapsed_ms",
        ):
            if key in summary:
                totals[key] = summary.get(key)
        payload["totals"] = totals
    return payload


def _extract_turn_timing_spans_from_payload(
    payload: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    spans: list[dict[str, Any]] = []
    for key in ("_timing_spans", "timing_spans"):
        value = payload.get(key)
        if isinstance(value, list):
            spans.extend(dict(item) for item in value if isinstance(item, Mapping))
    timing_trace = payload.get("turn_timing_trace")
    if isinstance(timing_trace, Mapping):
        trace_spans = timing_trace.get("spans")
        if isinstance(trace_spans, list):
            spans.extend(
                dict(item) for item in trace_spans if isinstance(item, Mapping)
            )
    return merge_timing_spans([], spans)


def _canonicalise_turn_execution_stage_id(stage: Any) -> str | None:
    clean_stage = _progress_str(stage)
    if not clean_stage:
        return None
    recovery_stage = _canonicalise_recovery_stage(clean_stage)
    if recovery_stage:
        return recovery_stage
    if clean_stage == "workflow_discovery_complete":
        return "workflow_discovery"
    if clean_stage == "orchestrator_start":
        return "workflow_dispatch_prepare"
    if clean_stage == "orchestrator_end":
        return "workflow_dispatch"
    return clean_stage


def _extract_response_transformation_event_summary(
    response_transformations: Mapping[str, Any] | None,
    *,
    transform_name: str,
) -> dict[str, Any] | None:
    if not isinstance(response_transformations, Mapping):
        return None

    raw_transformations = response_transformations.get("transformations")
    if not isinstance(raw_transformations, list):
        return None

    matched_event: dict[str, Any] | None = None
    for raw_event in raw_transformations:
        if not isinstance(raw_event, Mapping):
            continue
        if _progress_str(raw_event.get("transform_name")) != transform_name:
            continue
        input_summary_raw = raw_event.get("input_summary")
        output_summary_raw = raw_event.get("output_summary")
        matched_event = {
            "transform_name": _progress_str(raw_event.get("transform_name")),
            "status": _progress_str(raw_event.get("status")),
            "source_path": _progress_str(raw_event.get("source_path")),
            "latency_ms": _progress_number(raw_event.get("latency_ms")),
            "model_id": _progress_str(raw_event.get("model_id")),
            "suppression_reason": _progress_str(raw_event.get("suppression_reason")),
            "error_class": _progress_str(raw_event.get("error_class")),
            "input_summary": (
                dict(input_summary_raw)
                if isinstance(input_summary_raw, Mapping)
                else {}
            ),
            "output_summary": (
                dict(output_summary_raw)
                if isinstance(output_summary_raw, Mapping)
                else {}
            ),
        }

    return matched_event


def _build_turn_execution_stage_diagnostics(
    *,
    diagnostic_events: list[dict[str, Any]],
    workflow_stage_path: Mapping[str, Any] | None,
    tool_history: list[dict[str, Any]],
    tool_observation_summary: Mapping[str, Any] | None,
    workflow_discovery: Mapping[str, Any] | None,
    latest_progress: Mapping[str, Any] | None,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None = None,
    response_transformations: Mapping[str, Any] | None = None,
    critic_verdict: Mapping[str, Any] | None = None,
    completion_gate_verdict: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    path_entries_raw = (
        workflow_stage_path.get("path")
        if isinstance(workflow_stage_path, Mapping)
        else None
    )
    path_entries = (
        [entry for entry in path_entries_raw if isinstance(entry, Mapping)]
        if isinstance(path_entries_raw, list)
        else []
    )
    if not path_entries:
        return []

    latest_progress_payload = (
        latest_progress if isinstance(latest_progress, Mapping) else {}
    )
    latest_stage_id = _canonicalise_live_runtime_stage(
        _progress_str(latest_progress_payload.get("phase"))
        or _progress_str(latest_progress_payload.get("stage"))
    )
    latest_terminal_state = _resolve_latest_turn_execution_activity_state(
        latest_progress_payload
    )
    live_stage_diagnostic_map = _extract_live_stage_diagnostic_map(
        latest_progress_payload
    )
    stage_diagnostics: list[dict[str, Any]] = []

    for stage_entry in path_entries:
        stage_id = _progress_str(stage_entry.get("stage_id")) or _progress_str(
            stage_entry.get("runtime_stage_normalised")
        )
        if not stage_id:
            continue

        stage_events = [
            entry
            for entry in diagnostic_events
            if (
                # JVNAUTOSCI-2133: Match by workflow_stage_id (authoritative
                # workflow stage label threaded through the chokepoint) when
                # present, falling back to phase/stage canonicalisation.
                _progress_str(entry.get("workflow_stage_id")) == stage_id
                or _canonicalise_turn_execution_stage_id(
                    entry.get("phase") or entry.get("stage")
                )
                == stage_id
            )
        ]
        latest_stage_event = stage_events[-1] if stage_events else None
        live_stage_payload = dict(live_stage_diagnostic_map.get(stage_id) or {})
        live_llm_exchange = _build_live_llm_exchange_summary(
            stage_id=stage_id,
            live_stage_payload=live_stage_payload,
            latest_stage_event=(
                latest_stage_event if isinstance(latest_stage_event, Mapping) else None
            ),
            latest_progress_payload=latest_progress_payload,
        )
        authority_summary = build_stage_authority_summary(
            stage_id=stage_id,
            aux_entries=aux_llm_calls,
        )
        llm_input_recorded = bool(authority_summary["llm_input_recorded"]) or bool(
            live_llm_exchange and live_llm_exchange.get("llm_input_recorded")
        )
        llm_output_recorded = bool(authority_summary["llm_output_recorded"]) or bool(
            live_llm_exchange and live_llm_exchange.get("llm_output_recorded")
        )
        llm_exchange_record_count = int(authority_summary["llm_exchange_record_count"])
        llm_exchange_entry_types = list(authority_summary["llm_exchange_entry_types"])
        llm_exchange_summaries = [
            dict(entry)
            for entry in authority_summary["llm_exchange_summaries"]
            if isinstance(entry, Mapping)
        ]
        llm_exchange_summary_truncated_count = int(
            authority_summary["llm_exchange_summary_truncated_count"]
        )
        latest_llm_exchange = (
            dict(authority_summary["latest_llm_exchange"])
            if isinstance(authority_summary["latest_llm_exchange"], Mapping)
            else None
        )
        if live_llm_exchange and llm_exchange_record_count == 0:
            llm_exchange_record_count = 1
            llm_exchange_entry_types = ["live_llm_request"]
            llm_exchange_summaries = [dict(live_llm_exchange)]
            latest_llm_exchange = dict(live_llm_exchange)
        workflow_routing_diagnostics = latest_progress_payload.get(
            "workflow_routing_diagnostics"
        )
        dispatch_payload = (
            workflow_routing_diagnostics.get("dispatch")
            if isinstance(workflow_routing_diagnostics, Mapping)
            else None
        )
        stage_payload: dict[str, Any] = {
            "stage_id": stage_id,
            "stage_label": _progress_str(stage_entry.get("stage_label"))
            or _progress_str(live_stage_payload.get("stage_label"))
            or stage_id.replace("_", " ").strip().title(),
            "event_count": int(
                _progress_number(live_stage_payload.get("event_count"))
                or len(stage_events)
            ),
            "latest_status": _progress_str(live_stage_payload.get("latest_status"))
            or (
                _progress_str(latest_stage_event.get("status"))
                if isinstance(latest_stage_event, Mapping)
                else None
            ),
            "latest_at_utc": _progress_str(live_stage_payload.get("latest_at_utc"))
            or (
                _progress_str(latest_stage_event.get("at_utc"))
                if isinstance(latest_stage_event, Mapping)
                else None
            ),
            "latest_result_summary": _progress_str(
                live_stage_payload.get("latest_result_summary")
            )
            or (
                _progress_str(latest_stage_event.get("result_summary"))
                if isinstance(latest_stage_event, Mapping)
                else None
            ),
            "latest_error": _progress_str(live_stage_payload.get("latest_error"))
            or (
                _progress_str(latest_stage_event.get("error"))
                if isinstance(latest_stage_event, Mapping)
                else None
            ),
            "llm_input_recorded": llm_input_recorded,
            "llm_output_recorded": llm_output_recorded,
            "has_recorded_llm_exchange": llm_input_recorded and llm_output_recorded,
            "llm_exchange_record_count": llm_exchange_record_count,
            "llm_exchange_entry_types": llm_exchange_entry_types,
            "llm_exchange_summaries": llm_exchange_summaries,
            "llm_exchange_summary_truncated_count": llm_exchange_summary_truncated_count,
            "latest_llm_exchange": latest_llm_exchange,
            "missing_recorded_llm_input": not llm_input_recorded,
            "missing_recorded_llm_output": not llm_output_recorded,
            "python_decision_count": int(authority_summary["python_decision_count"]),
            "possibly_inappropriate_python_code_use_count": int(
                authority_summary["possibly_inappropriate_python_code_use_count"]
            ),
            "python_decision_classes": list(
                authority_summary["python_decision_classes"]
            ),
            "python_decision_sources": list(
                authority_summary["python_decision_sources"]
            ),
            "latest_subtask": _progress_str(live_stage_payload.get("latest_subtask"))
            or (
                _progress_str(latest_stage_event.get("subtask"))
                if isinstance(latest_stage_event, Mapping)
                else None
            )
            or (
                _progress_str(latest_progress_payload.get("subtask"))
                if stage_id == latest_stage_id
                else None
            ),
            "latest_workflow_task": _progress_str(
                live_stage_payload.get("latest_workflow_task")
            )
            or (
                _progress_str(latest_stage_event.get("workflow_task"))
                if isinstance(latest_stage_event, Mapping)
                else None
            )
            or (
                _progress_str(latest_progress_payload.get("workflow_task"))
                if stage_id == latest_stage_id
                else None
            ),
            "latest_tool": _progress_str(live_stage_payload.get("latest_tool"))
            or (
                _progress_str(latest_stage_event.get("tool"))
                if isinstance(latest_stage_event, Mapping)
                else None
            )
            or (
                _progress_str(latest_progress_payload.get("tool"))
                if stage_id == latest_stage_id
                else None
            ),
        }

        if stage_id == "workflow_discovery" and isinstance(workflow_discovery, Mapping):
            stage_payload["workflow_discovery"] = dict(workflow_discovery)
        if stage_id in {"tool_plan", "tool_execute"} and isinstance(
            dispatch_payload, Mapping
        ):
            tool_execution = dispatch_payload.get("tool_execution")
            if isinstance(tool_execution, Mapping):
                stage_payload["tool_execution"] = dict(tool_execution)
        if stage_id == "tool_execute":
            summary_payload = (
                tool_observation_summary
                if isinstance(tool_observation_summary, Mapping)
                else {}
            )
            stage_payload["tool_call_count"] = int(
                _progress_number(summary_payload.get("tool_call_count")) or 0
            )
            stage_payload["tool_success_count"] = int(
                _progress_number(summary_payload.get("tool_success_count")) or 0
            )
            stage_payload["tool_failure_count"] = int(
                _progress_number(summary_payload.get("tool_failure_count")) or 0
            )
            stage_payload["tool_pending_count"] = int(
                _progress_number(summary_payload.get("tool_pending_count")) or 0
            )
            stage_payload["tool_call_start_count"] = int(
                _progress_number(summary_payload.get("tool_call_start_count")) or 0
            )
            stage_payload["tool_call_end_count"] = int(
                _progress_number(summary_payload.get("tool_call_end_count")) or 0
            )
            if tool_history:
                stage_payload["tool_history"] = [dict(entry) for entry in tool_history]
        if stage_id in {
            "workflow_dispatch_prepare",
            "workflow_dispatch",
            "plain_response",
        }:
            stage_payload.update(
                _resolve_workflow_selection_fields(
                    latest_progress_payload=latest_progress_payload,
                    live_stage_payload=live_stage_payload,
                    latest_stage_event=(
                        latest_stage_event
                        if isinstance(latest_stage_event, Mapping)
                        else None
                    ),
                    workflow_routing_diagnostics=(
                        workflow_routing_diagnostics
                        if isinstance(workflow_routing_diagnostics, Mapping)
                        else None
                    ),
                )
            )
            if isinstance(dispatch_payload, Mapping):
                pre_dispatch = dispatch_payload.get("pre_dispatch")
                if isinstance(pre_dispatch, Mapping):
                    stage_payload["pre_dispatch"] = dict(pre_dispatch)
                stage_payload["dispatch_terminal_status"] = _progress_str(
                    dispatch_payload.get("dispatch_terminal_status")
                )
                stage_payload["dispatch_terminal_failure_reason"] = _progress_str(
                    dispatch_payload.get("dispatch_terminal_failure_reason")
                )
                stage_payload["dispatch_terminal_failure_detail"] = _progress_str(
                    dispatch_payload.get("dispatch_terminal_failure_detail")
                )
                unresolved_required_inputs = dispatch_payload.get(
                    "dispatch_terminal_unresolved_required_inputs"
                )
                if isinstance(unresolved_required_inputs, list):
                    stage_payload["dispatch_terminal_unresolved_required_inputs"] = [
                        str(item).strip()
                        for item in unresolved_required_inputs
                        if str(item).strip()
                    ]

        if stage_id == "postcondition_critic":
            critic_payload = (
                dict(critic_verdict) if isinstance(critic_verdict, Mapping) else {}
            )
            if critic_payload:
                stage_payload["critic_verdict"] = critic_payload

        if stage_id == "completion_gate":
            gate_payload = (
                dict(completion_gate_verdict)
                if isinstance(completion_gate_verdict, Mapping)
                else {}
            )
            if not gate_payload:
                gate_payload = {}
                decision = _progress_str(
                    latest_progress_payload.get("completion_gate_decision")
                )
                decision_reason = _progress_str(
                    latest_progress_payload.get("completion_gate_decision_reason")
                )
                if decision:
                    gate_payload["decision"] = decision
                if decision_reason:
                    gate_payload["decision_reason"] = decision_reason
                requires_follow_up = latest_progress_payload.get(
                    "completion_gate_requires_follow_up"
                )
                if isinstance(requires_follow_up, bool):
                    gate_payload["requires_follow_up"] = requires_follow_up
                safe_to_claim_completion = latest_progress_payload.get(
                    "completion_gate_safe_to_claim_completion"
                )
                if isinstance(safe_to_claim_completion, bool):
                    gate_payload["safe_to_claim_completion"] = safe_to_claim_completion
                blocking_effect_ids = latest_progress_payload.get(
                    "completion_gate_blocking_effect_ids"
                )
                if isinstance(blocking_effect_ids, list):
                    gate_payload["blocking_effect_ids"] = [
                        str(item).strip()
                        for item in blocking_effect_ids
                        if str(item).strip()
                    ]
            if gate_payload:
                stage_payload["completion_gate"] = gate_payload

        if stage_id == "screen_backfill":
            transformation = _extract_response_transformation_event_summary(
                response_transformations,
                transform_name="screen_backfill",
            )
            if transformation:
                stage_payload["response_transformation"] = transformation

        if stage_id == "narration":
            transformation = _extract_response_transformation_event_summary(
                response_transformations,
                transform_name="spoken_backfill",
            )
            if transformation:
                stage_payload["response_transformation"] = transformation

        if (
            latest_terminal_state in {"success", "failure"}
            and stage_id == latest_stage_id
        ):
            stage_payload["terminal_state"] = latest_terminal_state

        stage_diagnostics.append(stage_payload)

    return stage_diagnostics


def _build_turn_execution_mcp_access(
    *,
    request_id: str,
    session_id: str | None,
    namespace: str | None,
    history_owner_user_id: str | None,
    organisation_concept_id: str | None,
    workflow_routing: Mapping[str, Any] | None = None,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None = None,
    selected_workflow_trace: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from ...services.conversation_scope_binding_service import (
        build_conversation_scope_binding,
    )
    from ...services.turn_execution_diagnostics_service import (
        build_workflow_execution_trace_mcp_access_refs,
    )

    def _tool_call_descriptor(
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "arguments": {
                key: value for key, value in arguments.items() if value is not None
            },
        }
        if isinstance(purpose, str) and purpose.strip():
            payload["purpose"] = purpose.strip()
        return payload

    access: dict[str, Any] = {
        "turn_execution_get_diagnostics": _tool_call_descriptor(
            "turn_execution_get_diagnostics",
            {"request_id": request_id, "namespace": namespace},
            purpose="Fetch the persisted full turn-execution diagnostics payload.",
        ),
        "turn_execution_get": _tool_call_descriptor(
            "turn_execution_get",
            {"request_id": request_id, "namespace": namespace},
            purpose="Fetch the projected turn-execution record for this request.",
        ),
    }

    if isinstance(session_id, str) and session_id.strip():
        conversation_ref = build_conversation_scope_binding(
            chat_session_id=session_id,
            history_owner_user_id=history_owner_user_id,
            read_namespace=namespace,
            organisation_concept_id=organisation_concept_id,
        )
        access["conversation_telemetry_get_locator"] = _tool_call_descriptor(
            "conversation_telemetry_get_locator",
            {
                "conversation_ref": conversation_ref,
                "namespace": namespace,
                "organisation_concept_id": organisation_concept_id,
            },
            purpose="Fetch the compact conversation locator for the surrounding chat session.",
        )
        access["chat_history_get_segments"] = _tool_call_descriptor(
            "chat_history_get_segments",
            {
                "conversation_ref": conversation_ref,
                "namespace": namespace,
                "include_debug": True,
                "organisation_concept_id": organisation_concept_id,
            },
            purpose="Fetch the stored transcript segments and embedded debug payloads for this session.",
        )

    workflow_trace_payload: dict[str, Any] = {}
    if isinstance(workflow_routing, Mapping):
        workflow_trace_payload["workflow_routing"] = dict(workflow_routing)
    if isinstance(selected_workflow_trace, Mapping):
        workflow_trace_payload["selected_workflow_trace"] = dict(
            selected_workflow_trace
        )
    if isinstance(aux_llm_calls, Sequence) and not isinstance(
        aux_llm_calls, (str, bytes, bytearray)
    ):
        workflow_trace_payload["aux_llm_calls"] = [
            dict(entry) for entry in aux_llm_calls if isinstance(entry, Mapping)
        ]
    workflow_traces = build_workflow_execution_trace_mcp_access_refs(
        workflow_trace_payload
    )
    if workflow_traces:
        access["workflow_execution_traces"] = workflow_traces
        primary_trace = next(
            (
                trace
                for trace in workflow_traces
                if trace.get("trace_role") == "selected_workflow"
            ),
            workflow_traces[0],
        )
        primary_access = primary_trace.get("mcp_access")
        if isinstance(primary_access, Mapping):
            access["workflow_get_execution_trace"] = dict(primary_access)

    return access


def _build_model_resolution_summary(
    aux_llm_calls: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """JVNAUTOSCI-2373: aggregate per-stage model fallback telemetry.

    Walks `workflow_model_policy_stage` aux entries and surfaces:
    - dead-candidate cache contents (skipped per stage, with reasons)
    - per-stage requested vs effective model
    - count of attempts that were skipped due to prior-turn failures

    Also emits ``user_settings_warning`` when a candidate originating from
    user-settings (``active_llm`` source) was classified dead, since that
    is the user-visible "your selected model is broken" signal.
    """
    if not isinstance(aux_llm_calls, list):
        return None

    stage_entries: list[dict[str, Any]] = []
    skipped_summary: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    total_skipped = 0
    user_settings_dead: list[dict[str, Any]] = []

    for entry in aux_llm_calls:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "workflow_model_policy_stage":
            continue
        stage_name = entry.get("stage")
        requested_model = entry.get("requested_model")
        effective_model = entry.get("effective_model")
        model_source = entry.get("model_source")
        skipped = entry.get("skipped_candidates")
        skipped_list = skipped if isinstance(skipped, list) else []
        stage_entries.append(
            {
                "stage": stage_name,
                "requested_model": requested_model,
                "effective_model": effective_model,
                "model_source": model_source,
                "fallback_used": bool(entry.get("fallback_used")),
                "fallback_attempt_count": entry.get("fallback_attempt_count"),
                "skipped_candidate_count": len(skipped_list),
            }
        )
        for sk in skipped_list:
            if not isinstance(sk, dict):
                continue
            total_skipped += 1
            key = (sk.get("provider"), sk.get("model"))
            existing = skipped_summary.get(key)
            if existing is None:
                skipped_summary[key] = {
                    "provider": sk.get("provider"),
                    "model": sk.get("model"),
                    "host": sk.get("host"),
                    "failure_class": sk.get("failure_class"),
                    "failure_kind": sk.get("failure_kind"),
                    "source": sk.get("source"),
                    "skipped_in_stages": [stage_name] if stage_name else [],
                }
            else:
                stages_list = existing.setdefault("skipped_in_stages", [])
                if stage_name and stage_name not in stages_list:
                    stages_list.append(stage_name)
            if sk.get("source") == "active_llm":
                user_settings_dead.append(
                    {
                        "provider": sk.get("provider"),
                        "model": sk.get("model"),
                        "failure_class": sk.get("failure_class"),
                    }
                )

    if not stage_entries:
        return None

    summary: dict[str, Any] = {
        "stages": stage_entries,
        "dead_candidates": list(skipped_summary.values()),
        "total_skipped_attempts": total_skipped,
    }
    if user_settings_dead:
        seen: set[tuple[Any, Any]] = set()
        unique: list[dict[str, Any]] = []
        for item in user_settings_dead:
            key = (item.get("provider"), item.get("model"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        summary["user_settings_warning"] = {
            "message": (
                "Your selected default model is currently unavailable; "
                "Von used a fallback model for this turn."
            ),
            "dead_active_llm_candidates": unique,
        }
    return summary


def _build_turn_execution_diagnostics(
    *,
    request_id: str | None,
    prompt_text: str | None,
    elapsed_ms: float | int | None = None,
    tool_progress_state: dict[str, Any] | None = None,
    workflow_discovery: dict[str, Any] | None = None,
    workflow_routing: dict[str, Any] | None = None,
    generated_at_utc: str | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
    aux_llm_calls: list[dict[str, Any]] | None = None,
    tool_invocations: Sequence[Mapping[str, Any]] | None = None,
    timing_spans: Sequence[Mapping[str, Any]] | None = None,
    response_transformations: Mapping[str, Any] | None = None,
    critic_verdict: Mapping[str, Any] | None = None,
    completion_gate_verdict: Mapping[str, Any] | None = None,
    mcp_access: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    code_version_details = get_runtime_code_version_info()

    prompt_preview = (
        prompt_text[:_TURN_EXECUTION_DIAGNOSTICS_PROMPT_PREVIEW_LIMIT]
        if isinstance(prompt_text, str)
        else None
    )

    latest_progress = (
        dict(tool_progress_state) if isinstance(tool_progress_state, dict) else None
    )

    diagnostic_events: list[dict[str, Any]] = []
    if isinstance(latest_progress, dict):
        raw_events = latest_progress.get("diagnostic_events")
        if isinstance(raw_events, list):
            diagnostic_events = [
                cast(dict[str, Any], entry)
                for entry in raw_events
                if isinstance(entry, dict)
            ][-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT:]

    effective_elapsed = _progress_number(elapsed_ms)
    if effective_elapsed is None and isinstance(latest_progress, dict):
        effective_elapsed = _progress_number(latest_progress.get("elapsed_ms"))
    elapsed_ms_value = (
        int(max(0.0, effective_elapsed)) if effective_elapsed is not None else None
    )

    workflow_payload = workflow_discovery
    if workflow_payload is None and isinstance(latest_progress, dict):
        progress_workflow = latest_progress.get("workflow_discovery")
        if isinstance(progress_workflow, dict):
            workflow_payload = dict(progress_workflow)
    workflow_routing_payload = (
        workflow_routing if isinstance(workflow_routing, dict) else None
    )
    if workflow_routing_payload is None and isinstance(latest_progress, dict):
        progress_workflow_routing = latest_progress.get("workflow_routing")
        if isinstance(progress_workflow_routing, dict):
            workflow_routing_payload = dict(progress_workflow_routing)

    routing_aux_llm_calls = aux_llm_calls if isinstance(aux_llm_calls, list) else None
    if routing_aux_llm_calls is None and isinstance(latest_progress, dict):
        progress_routing_aux = latest_progress.get("workflow_routing_aux")
        if isinstance(progress_routing_aux, list):
            routing_aux_llm_calls = [
                cast(dict[str, Any], entry)
                for entry in progress_routing_aux
                if isinstance(entry, dict)
            ]

    effective_request_id = _progress_str(request_id)
    if effective_request_id is None and isinstance(latest_progress, dict):
        effective_request_id = _progress_str(latest_progress.get("request_id"))

    phase_history = _extract_phase_history_from_progress_state(
        latest_progress,
        diagnostic_events=diagnostic_events,
    )
    runtime_stages = [entry.get("phase") for entry in phase_history]
    selected_workflow_id = (
        _progress_str(latest_progress.get("selected_workflow_id"))
        if isinstance(latest_progress, dict)
        else None
    )
    workflow_stage_path = _extract_workflow_stage_path_from_progress(latest_progress)
    if workflow_stage_path is None:
        workflow_stage_path = build_conversation_turn_stage_path(
            runtime_stages=runtime_stages,
            workflow_id=selected_workflow_id,
            selected_workflow_id=selected_workflow_id,
        )
    llm_call_entries = [
        cast(dict[str, Any], entry)
        for entry in (llm_calls or [])
        if isinstance(entry, dict)
    ]
    stage_authority_llm_entries: list[dict[str, Any]] = []
    if isinstance(routing_aux_llm_calls, list):
        stage_authority_llm_entries.extend(
            cast(dict[str, Any], entry)
            for entry in routing_aux_llm_calls
            if isinstance(entry, dict)
        )
    stage_authority_llm_entries.extend(llm_call_entries)
    tool_observation_summary = derive_tool_observations_from_diagnostic_events(
        diagnostic_events,
        limit=_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT,
    )
    progress_tool_observation_summary = _extract_tool_observation_summary_from_progress(
        latest_progress
    )
    if progress_tool_observation_summary is not None:
        tool_observation_summary = progress_tool_observation_summary
    tool_history = [
        cast(dict[str, Any], entry)
        for entry in (tool_observation_summary.get("tool_history") or [])
        if isinstance(entry, dict)
    ]
    extra_timing_spans = _extract_turn_timing_spans_from_payload(latest_progress)
    if isinstance(timing_spans, Sequence) and not isinstance(
        timing_spans, (str, bytes, bytearray)
    ):
        extra_timing_spans = merge_timing_spans(extra_timing_spans, timing_spans)
    tool_invocation_entries = [
        cast(Mapping[str, Any], entry)
        for entry in (tool_invocations or [])
        if isinstance(entry, Mapping)
    ]
    turn_timing_trace = build_turn_timing_trace(
        request_id=effective_request_id,
        phase_history=phase_history,
        diagnostic_events=diagnostic_events,
        llm_calls=llm_call_entries,
        tool_history=tool_history,
        tool_invocations=tool_invocation_entries,
        response_transformations=response_transformations,
        extra_spans=extra_timing_spans,
        elapsed_ms_value=elapsed_ms_value,
    )
    timing_breakdown = _attach_turn_timing_trace_to_breakdown(
        _build_timing_breakdown(
            diagnostic_events=diagnostic_events,
            phase_history=phase_history,
            llm_calls=llm_call_entries,
            elapsed_ms_value=elapsed_ms_value,
        ),
        turn_timing_trace,
    )
    stage_diagnostics = _build_turn_execution_stage_diagnostics(
        diagnostic_events=diagnostic_events,
        workflow_stage_path=workflow_stage_path,
        tool_history=tool_history,
        tool_observation_summary=tool_observation_summary,
        workflow_discovery=workflow_payload,
        latest_progress=latest_progress,
        aux_llm_calls=stage_authority_llm_entries,
        response_transformations=response_transformations,
        critic_verdict=critic_verdict,
        completion_gate_verdict=completion_gate_verdict,
    )
    progress_events = _build_progress_events_from_phase_history(
        phase_history,
        latest_progress=latest_progress,
    )
    activity_history = _build_activity_history_from_phase_history(
        phase_history,
        stage_diagnostics=stage_diagnostics,
        latest_progress=latest_progress,
    )
    workflow_routing_diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery=workflow_payload,
        workflow_routing=workflow_routing_payload,
        turn_execution_diagnostics={
            "latest_progress": latest_progress,
            "phase_history": phase_history,
            "workflow_stage_path": workflow_stage_path,
        },
        aux_llm_calls=routing_aux_llm_calls,
    )

    return {
        "schema_version": "turn_execution_diagnostics.v1",
        "generated_at_utc": _progress_str(generated_at_utc) or _now_utc_iso(),
        "request_id": effective_request_id,
        "code_version": _progress_str(code_version_details.get("version")),
        "code_version_details": code_version_details,
        "elapsed_ms": elapsed_ms_value,
        "prompt_preview": prompt_preview,
        "latest_progress": latest_progress,
        "progress_events": progress_events,
        "activity_history": activity_history,
        "phase_history": phase_history,
        "tool_history": tool_history,
        "tool_call_count": int(
            _progress_number(tool_observation_summary.get("tool_call_count")) or 0
        ),
        "tool_success_count": int(
            _progress_number(tool_observation_summary.get("tool_success_count")) or 0
        ),
        "tool_failure_count": int(
            _progress_number(tool_observation_summary.get("tool_failure_count")) or 0
        ),
        "tool_pending_count": int(
            _progress_number(tool_observation_summary.get("tool_pending_count")) or 0
        ),
        "tool_call_start_count": int(
            _progress_number(tool_observation_summary.get("tool_call_start_count")) or 0
        ),
        "tool_call_end_count": int(
            _progress_number(tool_observation_summary.get("tool_call_end_count")) or 0
        ),
        "workflow_discovery": workflow_payload,
        "workflow_routing_diagnostics": workflow_routing_diagnostics,
        "llm_calls": llm_call_entries,
        "turn_timing_trace": turn_timing_trace,
        "aux_llm_calls": (
            [
                cast(dict[str, Any], entry)
                for entry in routing_aux_llm_calls
                if isinstance(entry, dict)
            ]
            if isinstance(routing_aux_llm_calls, list)
            else []
        ),
        "critic_verdict": (
            dict(critic_verdict) if isinstance(critic_verdict, Mapping) else None
        ),
        "completion_gate": (
            dict(completion_gate_verdict)
            if isinstance(completion_gate_verdict, Mapping)
            else None
        ),
        "response_transformations": (
            dict(response_transformations)
            if isinstance(response_transformations, Mapping)
            else None
        ),
        "workflow_stage_model": build_conversation_turn_stage_model_snapshot(),
        "workflow_stage_path": workflow_stage_path,
        "stage_diagnostics": stage_diagnostics,
        "timing_breakdown": timing_breakdown,
        "mcp_access": (dict(mcp_access) if isinstance(mcp_access, Mapping) else None),
        "model_resolution_summary": _build_model_resolution_summary(
            routing_aux_llm_calls
        ),
    }


def _truncate_diagnostic_export_string(value: str) -> str:
    if len(value) <= _DIAGNOSTIC_EXPORT_STRING_LIMIT:
        return value
    truncated_chars = len(value) - _DIAGNOSTIC_EXPORT_STRING_LIMIT
    return (
        f"{value[:_DIAGNOSTIC_EXPORT_STRING_LIMIT]}"
        f"... [truncated {truncated_chars} chars]"
    )


def _is_sensitive_diagnostic_export_key(key: str) -> bool:
    lowered = key.strip().lower()
    if not lowered:
        return False
    if lowered in _DIAGNOSTIC_EXPORT_EXACT_SENSITIVE_KEYS:
        return True
    if "token" in lowered and "count" not in lowered and "tokens_" not in lowered:
        return True
    for marker in ("secret", "password", "authorization", "cookie"):
        if marker in lowered:
            return True
    return False


def _summarise_diagnostic_export_collection_shape(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        keys = [str(item) for item in value.keys()][
            :_DIAGNOSTIC_EXPORT_COLLECTION_LIMIT
        ]
        return {
            "summary": _DIAGNOSTIC_EXPORT_SUMMARISED_VALUE,
            "type": "object",
            "key_count": len(value),
            "keys": keys,
        }
    if isinstance(value, list):
        return {
            "summary": _DIAGNOSTIC_EXPORT_SUMMARISED_VALUE,
            "type": "array",
            "item_count": len(value),
        }
    return {
        "summary": _DIAGNOSTIC_EXPORT_SUMMARISED_VALUE,
        "type": type(value).__name__,
    }


def _sanitise_diagnostic_export_payload(
    value: Any,
    *,
    depth: int = 0,
    parent_key: str | None = None,
) -> Any:
    if depth >= _DIAGNOSTIC_EXPORT_MAX_DEPTH:
        return {"summary": _DIAGNOSTIC_EXPORT_SUMMARISED_VALUE, "depth_limited": True}

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return _truncate_diagnostic_export_string(value)

    if isinstance(value, Mapping):
        sanitised: dict[str, Any] = {}
        for index, (raw_key, raw_item) in enumerate(value.items()):
            if index >= _DIAGNOSTIC_EXPORT_COLLECTION_LIMIT:
                sanitised["truncated_keys"] = (
                    len(value) - _DIAGNOSTIC_EXPORT_COLLECTION_LIMIT
                )
                break

            key = str(raw_key)
            if _is_sensitive_diagnostic_export_key(key):
                sanitised[key] = _DIAGNOSTIC_EXPORT_REDACTED_VALUE
                continue

            lowered_key = key.strip().lower()
            if lowered_key in _DIAGNOSTIC_EXPORT_STRUCTURAL_ONLY_KEYS:
                sanitised[key] = _summarise_diagnostic_export_collection_shape(raw_item)
                continue

            sanitised[key] = _sanitise_diagnostic_export_payload(
                raw_item,
                depth=depth + 1,
                parent_key=key,
            )
        return sanitised

    if isinstance(value, list):
        sanitised_items = []
        for index, raw_item in enumerate(value):
            if index >= _DIAGNOSTIC_EXPORT_COLLECTION_LIMIT:
                sanitised_items.append(
                    {
                        "summary": _DIAGNOSTIC_EXPORT_SUMMARISED_VALUE,
                        "truncated_items": len(value)
                        - _DIAGNOSTIC_EXPORT_COLLECTION_LIMIT,
                    }
                )
                break
            sanitised_items.append(
                _sanitise_diagnostic_export_payload(
                    raw_item,
                    depth=depth + 1,
                    parent_key=parent_key,
                )
            )
        return sanitised_items

    try:
        return _truncate_diagnostic_export_string(str(value))
    except Exception:
        return f"<{type(value).__name__}>"


def _normalise_tool_progress_scope_keys(
    scope_keys: Sequence[str] | str | None,
) -> list[str]:
    raw_scope_keys: list[str | None]
    if isinstance(scope_keys, str):
        raw_scope_keys = [scope_keys]
    elif isinstance(scope_keys, Sequence):
        raw_scope_keys = [
            str(item) if isinstance(item, str) else None for item in scope_keys
        ]
    else:
        raw_scope_keys = []

    deduped: list[str] = []
    seen: set[str] = set()
    for raw_scope_key in raw_scope_keys:
        clean_scope_key = _progress_str(raw_scope_key)
        if not clean_scope_key or clean_scope_key in seen:
            continue
        seen.add(clean_scope_key)
        deduped.append(clean_scope_key)
    return deduped


def _start_tool_progress_heartbeat(
    scope_keys: Sequence[str] | str, request_id: str
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.wait(float(_TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC)):
            try:
                active_scope_keys = _normalise_tool_progress_scope_keys(scope_keys)
                current = None
                current_ordering_key: tuple[int, float, float, float] | None = None
                for candidate_scope_key in active_scope_keys:
                    candidate = _get_tool_progress(candidate_scope_key, request_id)
                    if not isinstance(candidate, dict):
                        continue
                    ordering_key = _tool_progress_state_ordering_key(candidate)
                    if current is None or ordering_key > cast(
                        tuple[int, float, float, float], current_ordering_key
                    ):
                        current = dict(candidate)
                        current_ordering_key = ordering_key
                if not isinstance(current, dict):
                    continue
                status = (_progress_str(current.get("status")) or "").lower()
                if status in _TOOL_PROGRESS_TERMINAL_STATUSES:
                    break
                heartbeat_payload = {
                    "status": "heartbeat",
                    "request_id": request_id,
                    "heartbeat_interval_sec": int(
                        _TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC
                    ),
                }
                for candidate_scope_key in active_scope_keys:
                    _set_tool_progress(
                        candidate_scope_key,
                        request_id,
                        dict(heartbeat_payload),
                    )
            except Exception:
                # Heartbeat is best-effort and must never break user requests.
                continue

    thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"tool-progress-heartbeat-{request_id[:8]}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _stop_tool_progress_heartbeat(
    stop_event: threading.Event | None, thread: threading.Thread | None
) -> None:
    try:
        if stop_event is not None:
            stop_event.set()
    except Exception:
        pass
    try:
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.3)
    except Exception:
        pass


def _slugify_concept_id_for_key(concept_id: str) -> str:
    cleaned = (concept_id or "").strip()
    if cleaned.startswith("#V#"):
        cleaned = cleaned[3:]
    cleaned = cleaned.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def _normalise_tool_progress_window_session_id(value: Any) -> str | None:
    clean_value = _progress_str(value)
    if not clean_value:
        return None
    if len(clean_value) > 200:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", clean_value):
        return None
    return clean_value


def _get_tool_progress_scope_key() -> str:
    """Return a stable scope key for tool-progress lookup.

    - Authenticated: scope is user concept id
    - Unauthenticated: scope is the stable window-session header when present,
      otherwise a legacy Flask-session random token
    """

    user_concept_id = None
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if isinstance(user_concept_id, str) and user_concept_id.strip():
        return f"user:{user_concept_id.strip()}"

    window_session_id = _normalise_tool_progress_window_session_id(
        request.headers.get(_WINDOW_SESSION_HEADER_NAME)
    )
    if window_session_id:
        return f"{_TOOL_PROGRESS_WINDOW_SCOPE_PREFIX}{window_session_id}"

    if "tool_progress_scope" not in session:
        session["tool_progress_scope"] = secrets.token_urlsafe(16)

    return f"{_TOOL_PROGRESS_SESSION_SCOPE_PREFIX}{session.get('tool_progress_scope')}"


def _get_tool_progress_bootstrap_scope_key() -> str:
    """Return a lightweight early-request progress scope key.

    This avoids header-based authenticated-user validation so the first visible
    progress write can happen before any potentially slow identity lookup
    finishes. If the server session already carries an authenticated user,
    preserve that stable user-scoped key; otherwise prefer the current window.
    """

    session_user = _progress_str(session.get("user_concept_id"))
    if session_user:
        return f"user:{session_user}"

    window_session_id = _normalise_tool_progress_window_session_id(
        request.headers.get(_WINDOW_SESSION_HEADER_NAME)
    )
    if window_session_id:
        return f"{_TOOL_PROGRESS_WINDOW_SCOPE_PREFIX}{window_session_id}"

    if "tool_progress_scope" not in session:
        session["tool_progress_scope"] = secrets.token_urlsafe(16)

    return f"{_TOOL_PROGRESS_SESSION_SCOPE_PREFIX}{session.get('tool_progress_scope')}"


def _build_tool_progress_scope_candidates(
    *,
    explicit_scope_key: str | None = None,
    user_concept_id: str | None = None,
    window_session_id: str | None = None,
    anonymous_session_id: str | None = None,
) -> list[str]:
    candidates: list[str] = []

    explicit_scope = _progress_str(explicit_scope_key)
    if explicit_scope:
        candidates.append(explicit_scope)

    user_id = _progress_str(user_concept_id)
    if user_id:
        candidates.append(f"user:{user_id}")

    window_id = _normalise_tool_progress_window_session_id(window_session_id)
    if window_id:
        candidates.append(f"{_TOOL_PROGRESS_WINDOW_SCOPE_PREFIX}{window_id}")

    anonymous_id = _progress_str(anonymous_session_id)
    if anonymous_id:
        candidates.append(f"{_TOOL_PROGRESS_SESSION_SCOPE_PREFIX}{anonymous_id}")

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def _register_tool_progress_scope_aliases(
    *,
    request_id: str,
    primary_scope_key: str | None,
    mirror_scope_keys: list[str],
    user_concept_id: str | None = None,
    window_session_id: str | None = None,
    anonymous_session_id: str | None = None,
) -> list[str]:
    primary_scope = _progress_str(primary_scope_key)
    if not primary_scope:
        return list(mirror_scope_keys)

    current_state = _get_tool_progress(primary_scope, request_id)
    for candidate_scope_key in _build_tool_progress_scope_candidates(
        explicit_scope_key=primary_scope,
        user_concept_id=user_concept_id,
        window_session_id=window_session_id,
        anonymous_session_id=anonymous_session_id,
    ):
        if (
            candidate_scope_key == primary_scope
            or candidate_scope_key in mirror_scope_keys
        ):
            continue
        mirror_scope_keys.append(candidate_scope_key)
        if isinstance(current_state, dict):
            _set_tool_progress(candidate_scope_key, request_id, dict(current_state))
    return list(mirror_scope_keys)


def _tool_progress_state_ordering_key(
    state: Mapping[str, Any],
) -> tuple[int, float, float, float]:
    sequence_no = _progress_number(state.get("sequence_no"))
    updated_at_epoch = _progress_number(state.get("updated_at_epoch"))
    elapsed_ms = _progress_number(state.get("elapsed_ms"))
    has_ordering = any(
        value is not None for value in (sequence_no, updated_at_epoch, elapsed_ms)
    )
    return (
        1 if has_ordering else 0,
        float(sequence_no if sequence_no is not None else -1.0),
        float(updated_at_epoch if updated_at_epoch is not None else -1.0),
        float(elapsed_ms if elapsed_ms is not None else -1.0),
    )


def _resolve_tool_progress_state_from_scope_candidates(
    *,
    request_id: str,
    explicit_scope_key: str | None = None,
    user_concept_id: str | None = None,
    window_session_id: str | None = None,
    anonymous_session_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    best_state: dict[str, Any] | None = None
    best_scope_key: str | None = None
    best_ordering_key: tuple[int, float, float, float] | None = None
    candidate_scope_keys = _build_tool_progress_scope_candidates(
        explicit_scope_key=explicit_scope_key,
        user_concept_id=user_concept_id,
        window_session_id=window_session_id,
        anonymous_session_id=anonymous_session_id,
    )

    # Prefer any in-memory live progress before touching the persisted store.
    # This keeps alternate-scope fallback responsive during active browser turns
    # instead of blocking on a slow persisted-store miss for an earlier scope.
    for candidate_scope_key in candidate_scope_keys:
        candidate_state = _get_live_memory_tool_progress(
            candidate_scope_key, request_id
        )
        if not isinstance(candidate_state, dict):
            continue
        ordering_key = _tool_progress_state_ordering_key(candidate_state)
        if best_state is None or ordering_key > cast(
            tuple[int, float, float, float], best_ordering_key
        ):
            best_state = dict(candidate_state)
            best_scope_key = candidate_scope_key
            best_ordering_key = ordering_key

    if best_state is not None:
        return best_state, best_scope_key

    for candidate_scope_key in candidate_scope_keys:
        candidate_state = _get_tool_progress(candidate_scope_key, request_id)
        if not isinstance(candidate_state, dict):
            continue
        ordering_key = _tool_progress_state_ordering_key(candidate_state)
        if best_state is None or ordering_key > cast(
            tuple[int, float, float, float], best_ordering_key
        ):
            best_state = dict(candidate_state)
            best_scope_key = candidate_scope_key
            best_ordering_key = ordering_key

    return best_state, best_scope_key


@von_bp.route("/api/files/upload", methods=["POST"])
def upload_file_to_blob_store_and_vontology():
    """Upload a user-provided file into the configured blob store and register it in Vontology.

    Security: user identity is derived server-side via get_effective_user_concept_id().

    Multipart form-data:
      - file: the uploaded file

    Returns JSON:
      - success
      - uploaded: { concept_id, type_concept_id, sha256, size_bytes, content_type, original_filename }
      - storage: { backend, key, uri, content_type, size_bytes, metadata }
    """

    from werkzeug.utils import secure_filename

    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return (
            jsonify(
                {
                    "success": False,
                    "error": "missing_user_context",
                    "message": "Missing user context: establish an authenticated session first.",
                }
            ),
            401,
        )

    # Ensure the upload is associated with a stable chat session so it becomes part
    # of the same persisted history that /von/generate uses.
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    session_id = session["session_id"]

    if "file" not in request.files:
        return jsonify({"success": False, "error": "missing_file"}), 400

    uploaded = request.files.get("file")
    if not uploaded or not getattr(uploaded, "filename", None):
        return jsonify({"success": False, "error": "empty_upload"}), 400

    original_filename = str(uploaded.filename)
    safe_filename = secure_filename(original_filename) or "uploaded_file"
    content_type = getattr(uploaded, "mimetype", None) or None

    try:
        data = uploaded.read()
    except Exception as exc:
        current_app.logger.warning(f"[files/upload] Failed to read upload: {exc}")
        return jsonify({"success": False, "error": "read_failed"}), 400

    if not isinstance(data, (bytes, bytearray)) or not data:
        return jsonify({"success": False, "error": "empty_bytes"}), 400

    data_bytes = bytes(data)

    import hashlib

    sha256 = hashlib.sha256(data_bytes).hexdigest()
    size_bytes = len(data_bytes)

    from ...services.blob_uploads import BlobUploadError, put_bytes_durable

    user_slug = _slugify_concept_id_for_key(user_concept_id)
    blob_key = f"uploads/{user_slug}/{sha256}/{safe_filename}"
    uploaded_at = _now_utc_iso()

    try:
        stored = put_bytes_durable(
            key=blob_key,
            data=data_bytes,
            content_type=content_type,
            sha256=sha256,
            size_bytes=size_bytes,
            metadata={
                "original_filename": original_filename,
                "user_concept_id": user_concept_id.strip(),
                "uploaded_at": uploaded_at,
            },
        )
    except BlobUploadError as exc:
        current_app.logger.error(
            "[files/upload] Blob store upload failed: %s",
            exc,
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "blob_store_upload_failed",
                    "message": str(exc),
                }
            ),
            502,
        )

    blob_ref = stored.ref

    # --- Ensure KR infrastructure exists ---
    type_concept_id = "#V#computer_file_copy"

    try:
        from ...db.repositories.concepts_repository import ConceptsRepository
        from ...vontology.utils_vontology import (
            THING_PRIMARY_ID,
            create_vontology_concept,
            ensure_thing_exists_and_link_orphans,
        )

        if not ConceptsRepository.find_one({"concept_id": type_concept_id}):
            # Prefer a store-of-information parent if present; otherwise fall back to Thing.
            parent_id = (
                "#V#store_of_information"
                if ConceptsRepository.find_one(
                    {"concept_id": "#V#store_of_information"}
                )
                else THING_PRIMARY_ID
            )
            if parent_id == THING_PRIMARY_ID:
                # Best-effort: ensure Thing exists.
                ensure_thing_exists_and_link_orphans()

            created = create_vontology_concept(
                parent_id=parent_id,
                new_concept_name="Computer File Copy",
                create_as_instance=False,
                description=(
                    "A computer file copy is an information-bearing artefact representing a specific stored byte sequence "
                    "(for example an uploaded file stored in Von's blob store)."
                ),
                notes=(
                    "Created on-demand by Von's chat file upload flow. Instances typically have blob store metadata "
                    "(URI, key, content type, size, and hash) recorded as text relations."
                ),
            )
            if not created.get("success"):
                current_app.logger.warning(
                    "[files/upload] Failed to create Computer File Copy type: %s",
                    created.get("message"),
                )
    except Exception as exc:
        current_app.logger.warning(
            f"[files/upload] KR type ensure failed (continuing): {exc}"
        )

    # --- Create the file-copy instance concept ---
    instance_concept_id = f"#V#uploaded_file_copy_{uuid.uuid4().hex}"

    try:
        from ...services import concept_service
        from ...db.repositories.concepts_repository import ConceptsRepository
        from ...services.text_value_service import upsert_text_for_concept

        concept_service.create_concept(
            name=original_filename,
            concept_id=instance_concept_id,
            parent_concept_ids=[type_concept_id],
            create_as_instance=True,
            system_tags=["uploaded", "file", "blob_store"],
            attributes={
                "sha256": sha256,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "blob_backend": blob_ref.backend,
                "blob_key": blob_ref.key,
                "blob_uri": blob_ref.uri,
            },
        )

        # Scope visibility to the current user.
        ConceptsRepository.update_one(
            {"concept_id": instance_concept_id},
            {
                "$set": {
                    f"relationships.{CANONICAL_SPECIFIC_TO_USER_PREDICATE}": [
                        user_concept_id.strip()
                    ]
                }
            },
        )

        # Attach blob + metadata as text relations (authoritative)
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_original_filename",
            text=original_filename,
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_sha256",
            text=sha256,
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_size_bytes",
            text=str(size_bytes),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_upload_timestamp",
            text=str(uploaded_at),
            lang="en-NZ",
        )
        if content_type:
            upsert_text_for_concept(
                subject_concept_id=instance_concept_id,
                predicate="#V#has_mime_type",
                text=content_type,
                lang="en-NZ",
            )

        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_backend",
            text=str(blob_ref.backend),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_key",
            text=str(blob_ref.key),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_uri",
            text=str(blob_ref.uri),
            lang="en-NZ",
        )
    except Exception as exc:
        current_app.logger.error(
            f"[files/upload] Failed to register uploaded file in Vontology: {exc}",
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "vontology_register_failed",
                    "detail": str(exc),
                }
            ),
            500,
        )

    chat_history_recorded = _record_file_upload_in_chat_history(
        user_concept_id=user_concept_id.strip(),
        session_id=session_id,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        blob_backend=str(blob_ref.backend),
        blob_key=str(blob_ref.key),
        blob_uri=str(blob_ref.uri),
        file_copy_concept_id=instance_concept_id,
    )

    organisation_concept_id = session.get("organisation_concept_id")
    if (
        not isinstance(organisation_concept_id, str)
        or not organisation_concept_id.strip()
    ):
        organisation_concept_id = None
    else:
        organisation_concept_id = organisation_concept_id.strip()

    try:
        from ...services.workflow_event_integration_service import (
            maybe_launch_file_copy_uploaded_workflow,
        )

        workflow_event_launch = maybe_launch_file_copy_uploaded_workflow(
            file_copy_concept_id=instance_concept_id,
            uploaded_by_concept_id=user_concept_id.strip(),
            organisation_concept_id=organisation_concept_id,
            content_type=content_type,
            original_filename=original_filename,
            size_bytes=size_bytes,
            sha256=sha256,
            blob_uri=str(blob_ref.uri),
            uploaded_at_iso=uploaded_at,
            index_in_rag=True,
        )
    except Exception as exc:
        current_app.logger.warning(
            "[files/upload] Failed to launch file-copy upload workflow event: %s",
            exc,
        )
        workflow_event_launch = {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": "file_copy.uploaded",
            "reason": "workflow_event_launch_failed",
            "error": str(exc),
        }

    return (
        jsonify(
            {
                "success": True,
                "uploaded": {
                    "concept_id": instance_concept_id,
                    "type_concept_id": type_concept_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                    "original_filename": original_filename,
                },
                "storage": {
                    "backend": blob_ref.backend,
                    "key": blob_ref.key,
                    "uri": blob_ref.uri,
                    "content_type": blob_ref.content_type,
                    "size_bytes": blob_ref.size_bytes,
                    "metadata": blob_ref.metadata,
                },
                "chat_history_recorded": bool(chat_history_recorded),
                "workflow_event_launch": workflow_event_launch,
            }
        ),
        200,
    )


def _record_file_upload_in_chat_history(
    *,
    user_concept_id: str,
    session_id: str,
    original_filename: str,
    content_type: str | None,
    size_bytes: int,
    sha256: str,
    blob_backend: str,
    blob_key: str,
    blob_uri: str,
    file_copy_concept_id: str,
) -> bool:
    """Persist a durable upload record in chat history.

    We store human-readable text plus a compact JSON payload. This ensures the
    attachment can be rediscovered later (including the blob key/URI), assuming
    the user is authorised.
    """

    try:
        import json

        upload_summary = f"[UPLOAD] {original_filename} ({size_bytes} bytes)"
        assistant_lines: list[str] = [
            f"Attachment uploaded: {original_filename}",
            f"File copy concept: {file_copy_concept_id}",
            f"Blob URI: {blob_uri}",
            "(Blob access is subject to authorisation.)",
        ]

        payload = {
            "kind": "file_upload",
            "original_filename": original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "file_copy_concept_id": file_copy_concept_id,
            "blob": {
                "backend": blob_backend,
                "key": blob_key,
                "uri": blob_uri,
            },
        }

        assistant_text = (
            "\n".join(assistant_lines)
            + "\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )

        chat_history_service.add_message_to_history(
            user_concept_id,
            session_id,
            {"role": "user", "content": upload_summary},
        )
        chat_history_service.add_message_to_history(
            user_concept_id,
            session_id,
            {"role": "assistant", "content": assistant_text},
        )
        return True
    except Exception as exc:
        current_app.logger.warning(
            "[files/upload] Failed to record upload in chat history: %s", exc
        )
        return False


_FILE_DELETE_CONFIRM_PHRASE = "DELETE FILE"


def _get_effective_user_concept_id_for_file_routes() -> str | None:
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return None
    return user_concept_id.strip()


def _load_authorised_file_copy_concept_doc(
    *,
    concept_id: str,
    user_concept_id: str,
    log_prefix: str,
) -> dict[str, Any] | None:
    try:
        from ...db.repositories.concepts_repository import ConceptsRepository

        concept_doc = ConceptsRepository.find_one({"concept_id": concept_id})
    except Exception as exc:
        current_app.logger.warning("[%s] Concept lookup failed: %s", log_prefix, exc)
        concept_doc = None

    if not isinstance(concept_doc, dict):
        return None

    relationships = concept_doc.get("relationships")
    from ...security.access_control import _get_specific_to_user_values

    specific = (
        _get_specific_to_user_values(relationships)
        if isinstance(relationships, dict)
        else []
    )
    if specific and user_concept_id not in {
        str(x).strip() for x in specific if x is not None
    }:
        return None

    return concept_doc


@von_bp.route("/api/files/<path:file_copy_concept_id>/download", methods=["GET"])
def download_file_copy(file_copy_concept_id: str):
    """Download an uploaded file-copy by its Vontology concept id.

    Security: user identity is derived server-side via get_effective_user_concept_id().
    Access is restricted using canonical user visibility predicates on the file-copy concept.

    Path params:
      - file_copy_concept_id: URL-encoded concept id (e.g. %23V%23uploaded_file_copy_...)
    Query params:
      - concept_id: optional override (for callers that prefer query param)
    """

    user_concept_id = _get_effective_user_concept_id_for_file_routes()
    if user_concept_id is None:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "missing_user_context",
                    "message": "Missing user context: establish an authenticated session first.",
                }
            ),
            401,
        )

    # Allow query-parameter override so callers don't need to place the full id in the path.
    concept_id = request.args.get("concept_id") or file_copy_concept_id
    concept_id = unquote(str(concept_id or ""))
    concept_id = str(concept_id or "").strip()
    if not concept_id:
        return jsonify({"success": False, "error": "missing_concept_id"}), 400

    concept_doc = _load_authorised_file_copy_concept_doc(
        concept_id=concept_id,
        user_concept_id=user_concept_id,
        log_prefix="files/download",
    )
    if concept_doc is None:
        # Avoid leaking which concept IDs exist.
        return jsonify({"success": False, "error": "not_found"}), 404

    from ...services.computer_file_copy_service import fetch_file_copy_bytes

    result = fetch_file_copy_bytes(
        file_copy_concept_id=concept_id,
        user_concept_id=user_concept_id,
        allow_large=True,
        logger=current_app.logger,
    )
    if not isinstance(result, dict) or result.get("success") is not True:
        error = result.get("error") if isinstance(result, dict) else "not_found"
        if error == "not_found":
            return jsonify({"success": False, "error": "not_found"}), 404
        if error == "blob_fetch_failed":
            return jsonify({"success": False, "error": "blob_fetch_failed"}), 404
        if error == "file_too_large":
            return jsonify({"success": False, "error": "file_too_large"}), 413
        return jsonify({"success": False, "error": "not_found"}), 404

    info = result.get("info")
    data_bytes = result.get("data")
    if data_bytes is None:
        return jsonify({"success": False, "error": "blob_fetch_failed"}), 404

    import io

    download_name = "download"
    if info is not None and getattr(info, "original_filename", None):
        download_name = str(info.original_filename)
    else:
        name = concept_doc.get("name") if isinstance(concept_doc, dict) else None
        if isinstance(name, str) and name.strip():
            download_name = name.strip()

    mimetype = "application/octet-stream"
    if info is not None and getattr(info, "content_type", None):
        mimetype = str(info.content_type)
    resp = send_file(
        io.BytesIO(data_bytes),
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


@von_bp.route("/api/files/<path:file_copy_concept_id>", methods=["DELETE"])
def delete_file_copy(file_copy_concept_id: str):
    """Delete a file-copy concept and its backing blob bytes.

    This route is intentionally stricter than normal concept deletion:
    callers must supply a typed confirmation phrase to reduce accidental
    destructive operations.
    """

    user_concept_id = _get_effective_user_concept_id_for_file_routes()
    if user_concept_id is None:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "missing_user_context",
                    "message": "Missing user context: establish an authenticated session first.",
                }
            ),
            401,
        )

    concept_id = request.args.get("concept_id") or file_copy_concept_id
    concept_id = unquote(str(concept_id or ""))
    concept_id = str(concept_id or "").strip()
    if not concept_id:
        return jsonify({"success": False, "error": "missing_concept_id"}), 400

    payload = request.get_json(silent=True) or {}
    confirm_phrase = str(payload.get("confirm_phrase") or "").strip()
    if confirm_phrase != _FILE_DELETE_CONFIRM_PHRASE:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "confirmation_required",
                    "message": (
                        "Strong confirmation required. "
                        f"Set confirm_phrase to '{_FILE_DELETE_CONFIRM_PHRASE}'."
                    ),
                    "expected_confirm_phrase": _FILE_DELETE_CONFIRM_PHRASE,
                }
            ),
            400,
        )

    concept_doc = _load_authorised_file_copy_concept_doc(
        concept_id=concept_id,
        user_concept_id=user_concept_id,
        log_prefix="files/delete",
    )
    if concept_doc is None:
        return jsonify({"success": False, "error": "not_found"}), 404

    from ...services.computer_file_copy_service import delete_file_copy_blob_and_concept

    result = delete_file_copy_blob_and_concept(
        file_copy_concept_id=concept_id,
        logger=current_app.logger,
    )
    if not isinstance(result, dict):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "delete_failed",
                    "message": "Unexpected delete result.",
                }
            ),
            500,
        )

    if result.get("success") is True:
        return jsonify(result), 200

    error = str(result.get("error") or "delete_failed").strip().lower()
    if error == "not_found":
        return jsonify(result), 404
    if error == "blob_delete_failed":
        return jsonify(result), 502
    if error == "concept_delete_failed":
        return jsonify(result), 500
    return jsonify(result), 500


def _prune_tool_progress() -> None:
    cutoff = time.time() - _TOOL_PROGRESS_TTL_SEC
    with _TOOL_PROGRESS_LOCK:
        stale_keys = [
            key
            for key, value in _TOOL_PROGRESS.items()
            if isinstance(value, dict)
            and isinstance(value.get("updated_at_epoch"), (int, float))
            and float(value["updated_at_epoch"]) < cutoff
        ]
        for key in stale_keys:
            _TOOL_PROGRESS.pop(key, None)


def _cache_tool_progress_state(
    scope_key: str, request_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    cached = dict(payload)
    with _TOOL_PROGRESS_LOCK:
        _TOOL_PROGRESS[(scope_key, request_id)] = cached
    return cached


def _set_tool_progress(scope_key: str, request_id: str, update: dict[str, Any]) -> None:
    _prune_tool_progress()
    now_epoch = time.time()
    now_utc = _now_utc_iso()
    merged: dict[str, Any]
    with _TOOL_PROGRESS_LOCK:
        key = (scope_key, request_id)
        existing = _TOOL_PROGRESS.get(key)
        if not isinstance(existing, dict):
            existing = {}

        safe_update = dict(update or {})
        status = _progress_str(safe_update.get("status")) or _progress_str(
            existing.get("status")
        )
        if not status:
            status = "thinking"

        stage = _derive_progress_stage(safe_update, existing)
        event_kind = _derive_progress_event_kind(safe_update)

        request_started_epoch = _progress_number(
            existing.get("request_started_epoch")
        ) or _progress_number(existing.get("updated_at_epoch"))
        if request_started_epoch is None:
            request_started_epoch = now_epoch

        last_activity_epoch = _progress_number(
            existing.get("last_activity_epoch")
        ) or _progress_number(existing.get("updated_at_epoch"))
        if last_activity_epoch is None:
            last_activity_epoch = request_started_epoch

        last_stage_activity_epoch = _progress_number(
            existing.get("last_stage_activity_epoch")
        )
        if last_stage_activity_epoch is None:
            last_stage_activity_epoch = last_activity_epoch
        if event_kind != "heartbeat":
            last_stage_activity_epoch = now_epoch

        elapsed_ms = int(max(0.0, (now_epoch - request_started_epoch) * 1000.0))
        idle_ms = int(max(0.0, (now_epoch - last_activity_epoch) * 1000.0))
        stage_idle_ms = int(max(0.0, (now_epoch - last_stage_activity_epoch) * 1000.0))

        sequence_no = int(_progress_number(existing.get("sequence_no")) or 0) + 1

        existing_counters_raw = existing.get("counters")
        existing_counters: dict[str, Any]
        if isinstance(existing_counters_raw, dict):
            existing_counters = dict(cast(dict[str, Any], existing_counters_raw))
        else:
            existing_counters = {}
        explicit_tokens_streamed = _progress_number(safe_update.get("tokens_streamed"))
        existing_tokens_streamed = _progress_number(
            existing_counters.get("tokens_streamed", 0)
        )
        tokens_streamed = int(
            explicit_tokens_streamed
            if explicit_tokens_streamed is not None
            else (existing_tokens_streamed or 0)
        )

        existing_tools_started = _progress_number(
            existing_counters.get("tools_started")
        )
        existing_tools_completed = _progress_number(
            existing_counters.get("tools_completed")
        )
        tools_started = int(existing_tools_started or 0)
        tools_completed = int(existing_tools_completed or 0)
        if event_kind == "tool_call_start":
            tools_started += 1
        if event_kind == "tool_call_end":
            tools_completed += 1
        explicit_done = _progress_number(safe_update.get("tool_calls_done"))
        if explicit_done is not None:
            tools_started = max(tools_started, int(explicit_done))
            tools_completed = max(tools_completed, int(explicit_done))

        incoming_timing_spans: list[dict[str, Any]] = []
        raw_incoming_timing_spans = safe_update.get("timing_spans")
        if isinstance(raw_incoming_timing_spans, list):
            incoming_timing_spans.extend(
                dict(entry)
                for entry in raw_incoming_timing_spans
                if isinstance(entry, Mapping)
            )
        raw_incoming_trace = safe_update.get("turn_timing_trace")
        if isinstance(raw_incoming_trace, Mapping):
            raw_trace_spans = raw_incoming_trace.get("spans")
            if isinstance(raw_trace_spans, list):
                incoming_timing_spans.extend(
                    dict(entry)
                    for entry in raw_trace_spans
                    if isinstance(entry, Mapping)
                )

        merged = {**existing, **safe_update}
        if incoming_timing_spans:
            merged["_timing_spans"] = merge_timing_spans(
                _extract_turn_timing_spans_from_payload(existing),
                incoming_timing_spans,
            )
        elif isinstance(existing.get("_timing_spans"), list):
            merged["_timing_spans"] = merge_timing_spans(
                [],
                [
                    dict(entry)
                    for entry in existing["_timing_spans"]
                    if isinstance(entry, Mapping)
                ],
            )
        merged.pop("timing_spans", None)
        merged.pop("turn_timing_trace", None)
        if "success" in safe_update:
            success_value = safe_update.get("success")
            if isinstance(success_value, bool):
                merged["success"] = success_value
            else:
                merged.pop("success", None)
        else:
            # Terminal error/cancel events must not inherit an earlier success=True.
            if status.lower() in {"error", "cancelled"}:
                merged.pop("success", None)
        if "error" in safe_update:
            error_text = _progress_str(safe_update.get("error"))
            if error_text:
                merged["error"] = error_text
            else:
                merged.pop("error", None)
        else:
            # Avoid stale failure metadata leaking into later unrelated progress
            # events after a subsequent success or neutral heartbeat.
            merged.pop("error", None)
        if "error_class" in safe_update:
            error_class = _progress_str(safe_update.get("error_class"))
            if error_class:
                merged["error_class"] = error_class
            else:
                merged.pop("error_class", None)
        else:
            merged.pop("error_class", None)
        if "failure_kind" in safe_update:
            failure_kind = _progress_str(safe_update.get("failure_kind"))
            if failure_kind:
                merged["failure_kind"] = failure_kind
            else:
                merged.pop("failure_kind", None)
        else:
            merged.pop("failure_kind", None)
        if "error_code" in safe_update:
            error_code = _progress_str(safe_update.get("error_code"))
            if error_code:
                merged["error_code"] = error_code
            else:
                merged.pop("error_code", None)
        else:
            merged.pop("error_code", None)
        if "workflow_stage_id" in safe_update:
            workflow_stage_id = _progress_str(safe_update.get("workflow_stage_id"))
            if workflow_stage_id:
                merged["workflow_stage_id"] = workflow_stage_id
            else:
                merged.pop("workflow_stage_id", None)
        elif "phase" in safe_update or "stage" in safe_update:
            existing_workflow_stage_id = _canonicalise_turn_execution_stage_id(
                merged.get("workflow_stage_id")
            )
            incoming_stage_id = _canonicalise_turn_execution_stage_id(stage)
            if (
                existing_workflow_stage_id
                and incoming_stage_id
                and existing_workflow_stage_id != incoming_stage_id
            ):
                merged.pop("workflow_stage_id", None)
        merged["request_id"] = request_id
        merged["status"] = status
        merged["stage"] = stage
        merged["event_kind"] = event_kind
        goal_label = _normalise_progress_goal_label(merged.get("goal_label"))
        if goal_label:
            merged["goal_label"] = goal_label
        else:
            merged.pop("goal_label", None)
        merged["sequence_no"] = sequence_no
        merged["elapsed_ms"] = elapsed_ms
        merged["idle_ms"] = idle_ms
        merged["stage_idle_ms"] = stage_idle_ms
        merged["request_started_epoch"] = request_started_epoch
        merged["last_activity_epoch"] = now_epoch
        merged["last_activity_at_utc"] = now_utc
        merged["last_stage_activity_epoch"] = last_stage_activity_epoch
        if not _progress_str(merged.get("phase")):
            merged["phase"] = stage
        if not _progress_str(merged.get("phase_label")):
            merged["phase_label"] = _default_stage_label(stage)
        merged["stage_label"] = _default_stage_label(stage)

        subtask = _progress_str(merged.get("subtask"))
        if not subtask:
            subtask = _progress_str(merged.get("tool")) or _progress_str(
                merged.get("workflow_task")
            )
        merged["subtask"] = subtask

        merged["counters"] = {
            "tokens_streamed": max(0, tokens_streamed),
            "tools_started": max(0, tools_started),
            "tools_completed": max(0, tools_completed),
        }
        liveness = _derive_progress_liveness(merged, now_epoch=now_epoch)

        runtime_stages = _normalise_live_runtime_stage_sequence(
            existing.get("_workflow_runtime_stages")
        )
        runtime_stages = _append_live_runtime_stage(
            runtime_stages,
            _progress_str(merged.get("phase")) or _progress_str(merged.get("stage")),
        )
        merged["_workflow_runtime_stages"] = runtime_stages
        phase_history = _normalise_phase_history_entries(existing.get("_phase_history"))
        phase_history = _append_preserved_phase_history_entry(
            phase_history,
            phase=_progress_str(merged.get("phase"))
            or _progress_str(merged.get("stage")),
            phase_label=_progress_str(merged.get("phase_label"))
            or _progress_str(merged.get("stage_label")),
            timestamp_ms=int(now_epoch * 1000.0),
            at_utc=now_utc,
            sequence_no=sequence_no,
        )
        merged["_phase_history"] = phase_history
        merged["workflow_stage_path"] = (
            _build_live_workflow_stage_path_from_runtime_stages(
                runtime_stages,
                _progress_str(merged.get("selected_workflow_id")),
            )
        )

        existing_events = merged.get("diagnostic_events")
        if not isinstance(existing_events, list):
            existing_events = []
        event_entry = {
            "sequence_no": sequence_no,
            "at_utc": now_utc,
            "status": status,
            "event_kind": event_kind,
            "stage": stage,
            "phase": _progress_str(merged.get("phase")) or stage,
            "phase_label": _progress_str(merged.get("phase_label")),
            "stage_label": _progress_str(merged.get("stage_label")),
            # JVNAUTOSCI-2133: Preserve the authoritative workflow_stage_id
            # threaded through the chokepoint so downstream stage matchers can
            # group LLM exchange events by the workflow stage that owns them
            # rather than by the orchestrator-internal `stage`/`phase` label.
            "workflow_stage_id": _progress_str(merged.get("workflow_stage_id")),
            "goal_label": goal_label,
            "subtask": subtask,
            "tool": _progress_str(merged.get("tool")),
            "workflow_task": _progress_str(merged.get("workflow_task")),
            "batch_size": _progress_number(merged.get("batch_size")),
            "result_summary": _progress_str(merged.get("result_summary")),
            "idle_ms": idle_ms,
            "elapsed_ms": elapsed_ms,
            "liveness_state": liveness.get("liveness_state"),
            "liveness_reason": liveness.get("liveness_reason"),
            "stall_detected": liveness.get("stall_detected"),
        }
        progress_facts = _normalise_progress_facts(safe_update.get("progress_facts"))
        if "progress_facts" in safe_update:
            if progress_facts:
                merged["progress_facts"] = progress_facts
            else:
                merged.pop("progress_facts", None)
        call_id = _progress_str(safe_update.get("call_id"))
        if call_id:
            event_entry["call_id"] = call_id
        llm_exchange_id = _progress_str(safe_update.get("llm_exchange_id"))
        if llm_exchange_id:
            event_entry["llm_exchange_id"] = llm_exchange_id
        llm_request = safe_update.get("llm_request")
        if isinstance(llm_request, Mapping):
            event_entry["llm_request"] = {
                str(key): value
                for key, value in llm_request.items()
                if isinstance(key, str)
            }
        prompt_context_diagnostics = safe_update.get("prompt_context_diagnostics")
        if isinstance(prompt_context_diagnostics, Mapping):
            event_entry["prompt_context_diagnostics"] = {
                str(key): value
                for key, value in prompt_context_diagnostics.items()
                if isinstance(key, str)
            }
        llm_request_state = _progress_str(safe_update.get("llm_request_state"))
        if llm_request_state:
            event_entry["llm_request_state"] = llm_request_state
        llm_request_prepared_at_utc = _progress_str(
            safe_update.get("llm_request_prepared_at_utc")
        )
        if llm_request_prepared_at_utc:
            event_entry["llm_request_prepared_at_utc"] = llm_request_prepared_at_utc
        llm_request_sent_at_utc = _progress_str(
            safe_update.get("llm_request_sent_at_utc")
        )
        if llm_request_sent_at_utc:
            event_entry["llm_request_sent_at_utc"] = llm_request_sent_at_utc
        llm_first_output_at_utc = _progress_str(
            safe_update.get("llm_first_output_at_utc")
        )
        if llm_first_output_at_utc:
            event_entry["llm_first_output_at_utc"] = llm_first_output_at_utc
        llm_response_preview = safe_update.get("llm_response_preview")
        if isinstance(llm_response_preview, Mapping):
            event_entry["llm_response_preview"] = {
                str(key): value
                for key, value in llm_response_preview.items()
                if isinstance(key, str)
            }
        duration_ms = _progress_number(safe_update.get("duration_ms"))
        if duration_ms is not None:
            event_entry["duration_ms"] = int(max(0.0, duration_ms))
        model_name = _progress_str(safe_update.get("model"))
        if model_name:
            event_entry["model"] = model_name
        provider_name = _progress_str(safe_update.get("provider"))
        if provider_name:
            event_entry["provider"] = provider_name
        success_flag = safe_update.get("success")
        if isinstance(success_flag, bool):
            event_entry["success"] = success_flag
        if _progress_str(merged.get("error")):
            event_entry["error"] = str(merged.get("error"))
        error_code = _progress_str(merged.get("error_code"))
        if error_code:
            event_entry["error_code"] = error_code
        error_class = _progress_str(merged.get("error_class"))
        if error_class:
            event_entry["error_class"] = error_class
        candidate = merged.get("candidate")
        if isinstance(candidate, Mapping):
            event_entry["candidate"] = dict(candidate)
        fallback_attempt_no = _progress_number(merged.get("fallback_attempt_no"))
        if fallback_attempt_no is not None:
            event_entry["fallback_attempt_no"] = int(max(0.0, fallback_attempt_no))
        fallback_candidate_count = _progress_number(
            merged.get("fallback_candidate_count")
        )
        if fallback_candidate_count is not None:
            event_entry["fallback_candidate_count"] = int(
                max(0.0, fallback_candidate_count)
            )
        failure_kind = _progress_str(merged.get("failure_kind"))
        if failure_kind:
            event_entry["failure_kind"] = failure_kind
        selected_workflow_id = _progress_str(merged.get("selected_workflow_id"))
        if selected_workflow_id:
            event_entry["selected_workflow_id"] = selected_workflow_id
        selected_workflow_name = _progress_str(merged.get("selected_workflow_name"))
        if selected_workflow_name:
            event_entry["selected_workflow_name"] = selected_workflow_name
        selector_verdict = _progress_str(merged.get("workflow_selector_verdict"))
        if selector_verdict:
            event_entry["workflow_selector_verdict"] = selector_verdict
        selector_source = _progress_str(merged.get("workflow_selector_source"))
        if selector_source:
            event_entry["workflow_selector_source"] = selector_source
        workflow_selection_rationale = _progress_str(
            merged.get("workflow_selection_rationale")
        )
        if workflow_selection_rationale:
            event_entry["workflow_selection_rationale"] = workflow_selection_rationale
        workflow_match_count = _progress_number(merged.get("workflow_match_count"))
        if workflow_match_count is not None:
            event_entry["workflow_match_count"] = int(max(0.0, workflow_match_count))
        workflow_candidate_count = _progress_number(
            merged.get("workflow_candidate_count")
        )
        if workflow_candidate_count is not None:
            event_entry["workflow_candidate_count"] = int(
                max(0.0, workflow_candidate_count)
            )
        for field_name in (
            "selector_candidate_ids",
            "selector_discovered_workflow_ids",
            "selector_excluded_candidate_ids",
        ):
            raw_values = merged.get(field_name)
            if isinstance(raw_values, list):
                clean_values = [
                    item.strip()
                    for item in raw_values
                    if isinstance(item, str) and item.strip()
                ]
                if clean_values:
                    event_entry[field_name] = clean_values[:40]
        selector_candidate_count = _progress_number(
            merged.get("selector_candidate_count")
        )
        if selector_candidate_count is not None:
            event_entry["selector_candidate_count"] = int(
                max(0.0, selector_candidate_count)
            )
        selector_excluded_candidate_count = _progress_number(
            merged.get("selector_excluded_candidate_count")
        )
        if selector_excluded_candidate_count is not None:
            event_entry["selector_excluded_candidate_count"] = int(
                max(0.0, selector_excluded_candidate_count)
            )
        selected_workflow_execution_event = (
            _normalise_selected_workflow_execution_event(
                safe_update,
                sequence_no=sequence_no,
                at_utc=now_utc,
            )
        )
        if not progress_facts and selected_workflow_execution_event is not None:
            progress_facts = _normalise_progress_facts(
                selected_workflow_execution_event.get("progress_facts")
            )
            if progress_facts:
                merged["progress_facts"] = progress_facts
        if progress_facts:
            event_entry["progress_facts"] = progress_facts
        if selected_workflow_execution_event is not None:
            event_entry["selected_workflow_execution_event"] = dict(
                selected_workflow_execution_event
            )
            merged["selected_workflow_execution"] = (
                _append_selected_workflow_execution_event(
                    existing.get("selected_workflow_execution"),
                    selected_workflow_execution_event,
                )
            )
        trimmed_events = [
            *existing_events[-(_TOOL_PROGRESS_DIAGNOSTIC_EVENT_LIMIT - 1) :],
            event_entry,
        ]
        merged["diagnostic_events"] = trimmed_events

        stage_summaries = _normalise_live_stage_summary_map(
            existing.get("_stage_summaries")
        )
        # JVNAUTOSCI-2133: Prefer authoritative workflow_stage_id (threaded
        # through the chokepoint) over orchestrator-internal phase/stage labels
        # when bucketing live progress events into workflow stage summaries.
        live_stage_id = _progress_str(
            merged.get("workflow_stage_id")
        ) or _canonicalise_live_runtime_stage(
            _progress_str(merged.get("phase")) or _progress_str(merged.get("stage"))
        )
        if live_stage_id:
            existing_summary = dict(stage_summaries.get(live_stage_id) or {})
            latest_result_summary = _progress_str(event_entry.get("result_summary"))
            latest_subtask = _progress_str(event_entry.get("subtask"))
            latest_workflow_task = _progress_str(event_entry.get("workflow_task"))
            latest_tool = _progress_str(event_entry.get("tool"))
            existing_summary.update(
                {
                    "stage_id": live_stage_id,
                    "stage_label": _default_stage_label(live_stage_id),
                    "event_count": int(
                        _progress_number(existing_summary.get("event_count")) or 0
                    )
                    + 1,
                    "latest_status": _progress_str(event_entry.get("status")),
                    "latest_at_utc": _progress_str(event_entry.get("at_utc")),
                    "latest_result_summary": (
                        latest_result_summary
                        if latest_result_summary
                        else _progress_str(
                            existing_summary.get("latest_result_summary")
                        )
                    ),
                    "latest_error": _progress_str(event_entry.get("error")),
                    "latest_subtask": (
                        latest_subtask
                        if latest_subtask
                        else _progress_str(existing_summary.get("latest_subtask"))
                    ),
                    "latest_workflow_task": (
                        latest_workflow_task
                        if latest_workflow_task
                        else _progress_str(existing_summary.get("latest_workflow_task"))
                    ),
                    "latest_tool": (
                        latest_tool
                        if latest_tool
                        else _progress_str(existing_summary.get("latest_tool"))
                    ),
                }
            )
            if progress_facts:
                existing_summary["progress_facts"] = progress_facts
            stage_summaries[live_stage_id] = existing_summary
        merged["_stage_summaries"] = stage_summaries

        tool_summary_source = dict(existing) if isinstance(existing, dict) else {}
        for key_name in (
            "tool_history",
            "tool_call_count",
            "tool_success_count",
            "tool_failure_count",
            "tool_pending_count",
            "tool_call_start_count",
            "tool_call_end_count",
            "counters",
        ):
            if key_name in safe_update:
                tool_summary_source[key_name] = safe_update.get(key_name)
        merged.update(
            update_tool_observation_summary(
                tool_summary_source,
                event_entry,
                limit=_TOOL_PROGRESS_DIAGNOSTIC_EVENT_LIMIT,
            )
        )
        merged["diagnostic_summary"] = {
            "request_id": request_id,
            "event_count": len(trimmed_events),
            "latest_sequence_no": sequence_no,
            "latest_stage": stage,
            "latest_status": status,
            "elapsed_ms": elapsed_ms,
            "idle_ms": idle_ms,
            "last_activity_at_utc": now_utc,
            "counters": dict(merged["counters"]),
        }

        merged.update(liveness)
        merged["updated_at"] = now_utc
        merged["updated_at_epoch"] = now_epoch
        _TOOL_PROGRESS[key] = merged

    try:
        phase_for_terminal = _progress_str(merged.get("phase"))
        terminal_state = (
            str(merged.get("status") or "").strip().lower()
            in _TOOL_PROGRESS_TERMINAL_STATUSES
            or (phase_for_terminal or "").strip().lower()
            in _TOOL_PROGRESS_TERMINAL_PHASES
        )
        synchronous_durability = any(
            bool(merged.get(flag_name))
            for flag_name in (
                "synchronous_durability",
                "progress_synchronous_durability",
                "progress_persistence_synchronous",
            )
        )
        persistence_telemetry = queue_tool_progress_state_persistence(
            scope_key=scope_key,
            request_id=request_id,
            payload=merged,
            ttl_seconds=_TOOL_PROGRESS_TTL_SEC,
            terminal_state=terminal_state,
            synchronous_durability=synchronous_durability,
        )
        if isinstance(persistence_telemetry, Mapping):
            with _TOOL_PROGRESS_LOCK:
                current = _TOOL_PROGRESS.get((scope_key, request_id))
                if isinstance(current, dict) and current.get(
                    "sequence_no"
                ) == merged.get("sequence_no"):
                    current["progress_persistence"] = dict(persistence_telemetry)
    except Exception:
        pass


def _get_tool_progress(scope_key: str, request_id: str) -> dict[str, Any] | None:
    _prune_tool_progress()
    with _TOOL_PROGRESS_LOCK:
        value = _TOOL_PROGRESS.get((scope_key, request_id))
        if isinstance(value, dict):
            return dict(value)
    try:
        persisted = fetch_tool_progress_state(
            scope_key=scope_key, request_id=request_id
        )
    except Exception:
        persisted = None
    if isinstance(persisted, dict):
        return dict(_cache_tool_progress_state(scope_key, request_id, persisted))
    return None


def _get_live_memory_tool_progress(
    scope_key: str, request_id: str
) -> dict[str, Any] | None:
    _prune_tool_progress()
    with _TOOL_PROGRESS_LOCK:
        value = _TOOL_PROGRESS.get((scope_key, request_id))
        if isinstance(value, dict):
            return dict(value)
    return None


def _snapshot_tool_progress_for_request(
    scope_key: str | None, request_id: str | None
) -> dict[str, Any] | None:
    scope = _progress_str(scope_key)
    req = _progress_str(request_id)
    if not scope or not req:
        return None

    raw_state = _get_tool_progress(scope, req)
    if not isinstance(raw_state, dict):
        return None
    return _serialise_tool_progress_state(raw_state)


def _clear_tool_progress(scope_key: str, request_id: str) -> None:
    with _TOOL_PROGRESS_LOCK:
        _TOOL_PROGRESS.pop((scope_key, request_id), None)
    try:
        discard_queued_tool_progress_state(scope_key=scope_key, request_id=request_id)
    except Exception:
        pass
    try:
        delete_tool_progress_state(scope_key=scope_key, request_id=request_id)
    except Exception:
        pass


@von_bp.route("/progress/<request_id>", methods=["GET"])
def get_generation_progress(request_id: str):
    """Return the latest tool-execution progress for an in-flight generate() call."""

    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > 200
    ):
        return jsonify({"error": "Invalid request_id"}), 400

    window_session_id = _normalise_tool_progress_window_session_id(
        request.headers.get(_WINDOW_SESSION_HEADER_NAME)
    )
    header_user_concept_id = _normalise_concept_id(
        request.headers.get("X-User-Concept-ID")
    ) or _progress_str(request.headers.get("X-User-Concept-ID"))

    scope_key = ""
    resolved_user_concept_id = header_user_concept_id
    anonymous_session_id = None

    # Keep live progress polling as header-first as possible so browser turns do
    # not depend on Flask-session reads while /von/generate is still active.
    if resolved_user_concept_id:
        scope_key = f"user:{resolved_user_concept_id}"
    elif window_session_id:
        scope_key = f"{_TOOL_PROGRESS_WINDOW_SCOPE_PREFIX}{window_session_id}"
    else:
        scope_key = _get_tool_progress_scope_key()
        if scope_key.startswith("user:"):
            resolved_user_concept_id = _progress_str(scope_key[len("user:") :])
        else:
            resolved_user_concept_id = _progress_str(session.get("user_concept_id"))
        anonymous_session_id = _progress_str(session.get("tool_progress_scope"))

    state, resolved_scope_key = _resolve_tool_progress_state_from_scope_candidates(
        request_id=request_id.strip(),
        explicit_scope_key=scope_key,
        user_concept_id=resolved_user_concept_id,
        window_session_id=window_session_id,
        anonymous_session_id=anonymous_session_id,
    )
    if not state:
        try:
            show_tool_use_progress = bool(get_show_tool_use_during_thinking())
        except Exception:
            show_tool_use_progress = False

        if not show_tool_use_progress:
            return jsonify({"status": "disabled"}), 200

        return (
            jsonify(
                _build_pending_tool_progress_placeholder_payload(
                    request_id=request_id.strip(),
                    status_code=202,
                )
            ),
            202,
        )

    serialised_state = _serialise_tool_progress_state(state)
    if (
        isinstance(resolved_scope_key, str)
        and resolved_scope_key
        and resolved_scope_key != scope_key
    ):
        serialised_state["resolved_scope_key"] = resolved_scope_key
        serialised_state["progress_source"] = "alternate_scope_fallback"
    return jsonify(serialised_state), 200


# ----------------- Background Tasks (JVNAUTOSCI-1038) -----------------


def _extract_durable_display_text(
    display_elements: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(display_elements, Mapping):
        return None
    elements = display_elements.get("elements")
    if not isinstance(elements, list):
        return None
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        payload = element.get("payload")
        if not isinstance(payload, Mapping):
            continue
        text_value = payload.get("text")
        if isinstance(text_value, str) and text_value.strip():
            return text_value.strip()
    return None


def _build_durable_turn_background_result(instance: Any) -> dict[str, Any]:
    outputs = dict(instance.outputs) if isinstance(instance.outputs, Mapping) else {}
    display_elements = outputs.get("display_elements")
    if not isinstance(display_elements, Mapping):
        display_elements = None

    response_text = outputs.get("response")
    if not isinstance(response_text, str) or not response_text.strip():
        response_text = outputs.get("selected_workflow_user_response")
    if not isinstance(response_text, str) or not response_text.strip():
        response_text = outputs.get("final_response")
    if not isinstance(response_text, str) or not response_text.strip():
        response_text = outputs.get("response_text")
    if not isinstance(response_text, str) or not response_text.strip():
        response_text = outputs.get("current_response")
    if not isinstance(response_text, str) or not response_text.strip():
        response_text = _extract_durable_display_text(display_elements)
    if not isinstance(response_text, str) or not response_text.strip():
        response_text = outputs.get("response_preview")
    if not isinstance(response_text, str) or not response_text.strip():
        response_text = outputs.get("error")
    if not isinstance(response_text, str):
        response_text = ""

    session_id = outputs.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        inputs = instance.inputs if isinstance(instance.inputs, Mapping) else {}
        session_id = inputs.get("conversation_session_id")

    request_id = outputs.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        request_id = getattr(instance, "source_event_id", None)

    llm_debug: dict[str, Any] = {
        "response": response_text,
        "request_id": request_id,
        "workflow_instance_id": getattr(instance, "instance_id", None),
        "workflow_instance_status": getattr(
            getattr(instance, "status", None), "value", None
        )
        or str(getattr(instance, "status", "") or ""),
        "workflow_instance_current_state": getattr(instance, "current_state", None),
        "background_result_source": "durable_conversation_turn_instance",
    }
    for key in (
        "workflow_discovery",
        "workflow_routing",
        "turn_execution_record",
        "turn_execution_diagnostics",
        "response_transformations",
    ):
        value = outputs.get(key)
        if isinstance(value, Mapping):
            llm_debug[key] = dict(value)

    return {
        "request_id": request_id,
        "session_id": session_id,
        "conversation_session_id": session_id,
        "conversation_session_name": None,
        "conversation_session_created": False,
        "response": response_text,
        "response_channels": None,
        "llm_debug": llm_debug,
        "display_elements": dict(display_elements) if display_elements else None,
        "rag_trace": None,
        "background_result_source": "durable_conversation_turn_instance",
        "workflow_instance_id": getattr(instance, "instance_id", None),
    }


def _find_terminal_durable_turn_instance(task_id: str) -> Any | None:
    try:
        manager = get_instance_manager()
        instances = manager.list_instances(
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            source_event_type="conversation_turn",
            source_event_id=task_id,
            status=(
                WorkflowInstanceStatus.COMPLETED,
                WorkflowInstanceStatus.FAILED,
                WorkflowInstanceStatus.CANCELLED,
            ),
            limit=1,
        )
    except Exception as exc:
        current_app.logger.warning(
            "[background_task] Durable turn reconciliation failed for task %s: %s",
            task_id,
            exc,
        )
        return None
    return instances[0] if instances else None


def _reconcile_background_task_from_durable_turn(task_id: str, status: Any) -> Any:
    if status is not None and getattr(status, "status", None) == "completed":
        return status

    instance = _find_terminal_durable_turn_instance(task_id)
    if instance is None:
        return status

    instance_status = getattr(instance, "status", None)
    workflow_status = getattr(instance_status, "value", None) or str(
        instance_status or ""
    )
    if workflow_status == WorkflowInstanceStatus.FAILED.value:
        task_status = "failed"
    elif workflow_status == WorkflowInstanceStatus.CANCELLED.value:
        task_status = "cancelled"
    else:
        task_status = "completed"

    result = _build_durable_turn_background_result(instance)
    progress = {
        "status": task_status,
        "source": "durable_conversation_turn_instance",
        "workflow_instance_id": getattr(instance, "instance_id", None),
        "workflow_status": workflow_status,
        "workflow_current_state": getattr(instance, "current_state", None),
    }
    error = getattr(instance, "error", None) if task_status != "completed" else None
    marker = getattr(background_task_registry, "mark_terminal_external", None)
    if callable(marker):
        return marker(
            task_id,
            status=task_status,
            result=result,
            error=error,
            progress=progress,
            session_id=result.get("session_id"),
        )
    return status


@von_bp.route("/api/task/status/<task_id>", methods=["GET"])
def get_task_status(task_id: str):
    """Get the status of a background task.

    Returns:
        200: Task status dict
        400: Invalid task_id
        404: Task not found
    """
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
        return jsonify({"error": "Invalid task_id"}), 400

    task_id_clean = task_id.strip()
    status = background_task_registry.get_task_status(task_id_clean)
    status = _reconcile_background_task_from_durable_turn(task_id_clean, status)
    if status is None:
        return jsonify({"error": "Task not found", "task_id": task_id}), 404

    return jsonify(status.to_dict()), 200


def _serialise_background_orchestrator_result(result: Any) -> dict[str, Any]:
    """Return a stable background-task payload for OrchestratorResult values.

    Background `/von/generate` calls should preserve the same workflow-routing
    and workflow-execution telemetry that the synchronous route exposes under
    `llm_debug`, so long-running UI flows can be validated without relying on a
    single open HTTP request.
    """

    tool_invocations = (
        list(result.tool_invocations)
        if getattr(result, "tool_invocations", None)
        else []
    )
    aux_llm_calls = (
        list(result.aux_llm_calls) if getattr(result, "aux_llm_calls", None) else []
    )
    workflow_routing = _normalise_workflow_routing_payload(
        getattr(result, "workflow_routing", None)
    )

    serialised: dict[str, Any] = {
        "response_text": result.response_text,
        "extra_messages": (
            list(result.extra_messages)
            if getattr(result, "extra_messages", None)
            else []
        ),
        "tool_invocations": tool_invocations,
        "aux_llm_calls": aux_llm_calls,
    }

    llm_debug: dict[str, Any] = {
        "response": result.response_text,
        "tool_invocations": tool_invocations,
        "aux_llm_calls": aux_llm_calls,
    }

    if workflow_routing is not None:
        serialised["workflow_routing"] = workflow_routing
        llm_debug["workflow_routing"] = workflow_routing

    if hasattr(result, "llm_calls"):
        llm_calls = list(result.llm_calls)
        serialised["llm_calls"] = llm_calls
        llm_debug["llm_calls"] = llm_calls
    if hasattr(result, "llm_usage"):
        serialised["llm_usage"] = result.llm_usage
        llm_debug["llm_usage"] = result.llm_usage
    orchestrator_duration_ms = getattr(result, "orchestrator_duration_ms", None)
    if isinstance(orchestrator_duration_ms, (int, float)):
        serialised["orchestrator_duration_ms"] = float(orchestrator_duration_ms)
        llm_debug["orchestrator_duration_ms"] = float(orchestrator_duration_ms)
    if hasattr(result, "render_plan") and isinstance(result.render_plan, dict):
        render_plan = dict(result.render_plan)
        serialised["render_plan"] = render_plan
        llm_debug["render_plan"] = render_plan

    serialised["llm_debug"] = llm_debug
    return serialised


def _normalise_background_generate_result(result: Any) -> dict[str, Any]:
    """Materialise a `/von/generate` view result into a JSON payload.

    Background UI turns should execute the same synchronous generate logic
    inside the worker, then persist the final JSON body as the task result.
    This keeps the background path behaviourally aligned with the foreground
    route without requiring the HTTP request to stay open for the full turn.
    """

    response = make_response(result)
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {
            "response_text": response.get_data(as_text=True),
        }
    status_code = int(getattr(response, "status_code", 200) or 200)
    if status_code >= 400:
        raise RuntimeError(
            f"/von/generate background execution failed with status {status_code}: "
            f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
        )
    return payload


def _mark_background_generate_completed_if_ready(
    *,
    background_task_id: str | None,
    result_body: Mapping[str, Any],
    request_id: str,
    session_id: str | None,
    user_id: str | None,
    response_text: str | None,
) -> None:
    """Expose a completed background generate result before slow persistence.

    The supervised workflow may already have produced the user-visible answer
    before chat-history/debug persistence and durable telemetry finalisation
    finish. For background turns, make that completed answer available to
    polling clients immediately; the worker may still continue best-effort
    persistence, and any late failure is intentionally not allowed to overwrite
    the completed user-facing result.
    """

    if not isinstance(background_task_id, str) or not background_task_id.strip():
        return
    marker = getattr(background_task_registry, "mark_terminal_external", None)
    if not callable(marker):
        return
    response_preview = (
        response_text[:500]
        if isinstance(response_text, str) and response_text.strip()
        else None
    )
    progress = {
        "status": "completed",
        "source": "background_generate_success_body",
        "phase": "response_finalising",
        "phase_label": "Response ready",
        "request_id": request_id,
        "result_summary": response_preview or "Background generate response is ready.",
    }
    try:
        marker(
            background_task_id.strip(),
            status="completed",
            result=dict(result_body),
            progress=progress,
            session_id=session_id,
            user_id=user_id,
        )
    except Exception:
        current_app.logger.debug(
            "[background_task] Failed to mark completed generate result for %s",
            background_task_id,
            exc_info=True,
        )


def _mark_background_generate_completed_from_orchestrator_ready_progress(
    *,
    background_task_id: str | None,
    progress_payload: Mapping[str, Any],
    request_id: str,
    session_id: str | None,
    created_conversation_session_name: str | None,
    created_conversation_session: bool,
    user_id: str | None,
    rag_trace: Any,
) -> None:
    if str(progress_payload.get("status") or "").strip() != "orchestrator_result_ready":
        return
    response_text = progress_payload.get("response_text")
    if not isinstance(response_text, str) or not response_text.strip():
        return

    presenter_channels = _extract_presenter_channels(response_text)
    workflow_discovery = progress_payload.get("workflow_discovery")
    workflow_routing = progress_payload.get("workflow_routing")
    selected_workflow_trace = progress_payload.get("selected_workflow_trace")
    aux_llm_calls = progress_payload.get("aux_llm_calls")
    llm_calls = progress_payload.get("llm_calls")
    tool_invocations = progress_payload.get("tool_invocations")
    render_plan = progress_payload.get("render_plan")
    llm_debug_info: dict[str, Any] = {
        "interaction_timestamp_utc": _now_utc_iso(),
        "request_id": request_id,
        "model": progress_payload.get("model"),
        "response": response_text,
        "presenter_channels": presenter_channels,
        "tool_invocations": (
            list(tool_invocations) if isinstance(tool_invocations, list) else []
        ),
        "aux_llm_calls": list(aux_llm_calls) if isinstance(aux_llm_calls, list) else [],
        "llm_calls": list(llm_calls) if isinstance(llm_calls, list) else [],
        "workflow_discovery": (
            dict(workflow_discovery)
            if isinstance(workflow_discovery, Mapping)
            else None
        ),
        "workflow_routing": (
            dict(workflow_routing) if isinstance(workflow_routing, Mapping) else None
        ),
        "selected_workflow_trace": (
            dict(selected_workflow_trace)
            if isinstance(selected_workflow_trace, Mapping)
            else None
        ),
        "background_result_source": "orchestrator_result_ready_progress",
    }
    if isinstance(render_plan, Mapping):
        llm_debug_info["render_plan"] = dict(render_plan)

    result_body = _build_generate_success_body(
        request_id=request_id,
        session_id=session_id,
        created_conversation_session_name=created_conversation_session_name,
        created_conversation_session=created_conversation_session,
        response_text=response_text,
        presenter_channels=(
            presenter_channels if isinstance(presenter_channels, dict) else None
        ),
        llm_debug_info=llm_debug_info,
        display_elements_contract=None,
        rag_trace=rag_trace,
    )
    _mark_background_generate_completed_if_ready(
        background_task_id=background_task_id,
        result_body=result_body,
        request_id=request_id,
        session_id=session_id,
        user_id=user_id,
        response_text=response_text,
    )


def _resolve_generate_requested_model(
    data: Mapping[str, Any] | None,
    *,
    user_concept_id: str | None,
    org_concept_id: str | None,
    configured_model: Any,
) -> tuple[str | None, str | None]:
    """Resolve the effective model name and optional explicit provider override.

    Precedence:
    1. Explicit request model sent by the UI
    2. Scoped user/org LLM setting
    3. Legacy app-config model value

    The explicit request model is important for browser-local UI selections,
    including machine-local Ollama overrides that should not be persisted into
    shared scoped settings.
    """

    requested_model_name = data.get("model") if isinstance(data, Mapping) else None
    if isinstance(requested_model_name, str):
        requested_model_name = requested_model_name.strip() or None
    else:
        requested_model_name = None

    requested_provider_name = None
    if isinstance(data, Mapping):
        for provider_key in (
            "model_provider",
            "selected_model_provider",
            "provider",
        ):
            provider_value = data.get(provider_key)
            if isinstance(provider_value, str) and provider_value.strip():
                requested_provider_name = provider_value.strip().lower()
                break
    if requested_provider_name not in {"openai", "ollama", "gemini"}:
        requested_provider_name = None

    explicit_client_type = None
    model_name = None
    if requested_model_name:
        requested_openai_model = _extract_openai_model_id(requested_model_name)
        requested_ollama_model = _extract_ollama_model_id(requested_model_name)
        if requested_provider_name:
            explicit_client_type = requested_provider_name
            if requested_provider_name == "ollama":
                model_name = requested_ollama_model or requested_model_name
            elif requested_provider_name == "openai":
                model_name = requested_openai_model or requested_model_name
            else:
                model_name = requested_model_name
        elif requested_openai_model and _looks_like_openai_model(
            requested_openai_model
        ):
            explicit_client_type = "openai"
            model_name = requested_openai_model
        elif requested_ollama_model and _looks_like_ollama_model(requested_model_name):
            explicit_client_type = "ollama"
            model_name = requested_ollama_model
        else:
            model_name = requested_model_name
    if not model_name:
        model_name = get_active_model_name(
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
        )
    if (
        not model_name
        and isinstance(configured_model, str)
        and configured_model.strip()
    ):
        model_name = configured_model.strip()

    return model_name, explicit_client_type


@von_bp.route("/api/task/result/<task_id>", methods=["GET"])
def get_task_result(task_id: str):
    """Get the result of a completed background task.

    Returns:
        200: Task result (serialised OrchestratorResult or error)
        400: Invalid task_id
        404: Task not found
        409: Task not yet completed or failed
    """
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
        return jsonify({"error": "Invalid task_id"}), 400

    task_id_clean = task_id.strip()
    status = background_task_registry.get_task_status(task_id_clean)
    status = _reconcile_background_task_from_durable_turn(task_id_clean, status)
    if status is None:
        return jsonify({"error": "Task not found", "task_id": task_id}), 404

    if status.status == "failed":
        return (
            jsonify(
                {
                    "error": "Task failed",
                    "task_id": task_id,
                    "detail": status.error,
                }
            ),
            409,
        )

    if status.status not in ("completed",):
        return (
            jsonify(
                {
                    "error": "Task not completed",
                    "task_id": task_id,
                    "status": status.status,
                }
            ),
            409,
        )

    # Serialise OrchestratorResult if that's what we have
    result = status.result
    if hasattr(result, "response_text"):
        return (
            jsonify(
                {
                    "task_id": task_id,
                    "result": _serialise_background_orchestrator_result(result),
                }
            ),
            200,
        )

    # Generic result
    return jsonify({"task_id": task_id, "result": result}), 200


@von_bp.route("/api/task/cancel/<task_id>", methods=["POST"])
def cancel_task(task_id: str):
    """Request cancellation of a running background task.

    Note: Actual cancellation depends on the task implementation cooperating.

    Returns:
        200: Cancellation requested
        400: Invalid task_id
        404: Task not found or already completed
    """
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
        return jsonify({"error": "Invalid task_id"}), 400

    success = background_task_registry.request_cancellation(task_id.strip())
    if not success:
        return (
            jsonify(
                {
                    "error": "Cannot cancel task",
                    "task_id": task_id,
                    "detail": "Task not found or already completed",
                }
            ),
            404,
        )

    return (
        jsonify(
            {"success": True, "task_id": task_id, "message": "Cancellation requested"}
        ),
        200,
    )


@von_bp.route("/api/tasks", methods=["GET"])
def list_tasks():
    """List background tasks.

    Query params:
        status: Filter by status (pending, running, completed, failed, cancelled)
        session_id: Filter by session ID (Phase 4)
        user_id: Filter by user ID (Phase 4)

    Returns:
        200: List of task status dicts
    """
    status_filter = request.args.get("status")
    if status_filter and status_filter not in (
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
    ):
        return jsonify({"error": "Invalid status filter"}), 400

    session_id = request.args.get("session_id")
    user_id = request.args.get("user_id")

    tasks = background_task_registry.list_tasks(
        status_filter=status_filter,
        session_id=session_id,
        user_id=user_id,
    )
    return jsonify({"tasks": tasks}), 200


# ----------------- End Background Tasks -----------------


def _truncate_large_tool_results(
    messages: list[dict], max_tool_content_chars: int = 5000
) -> list[dict]:
    """
    Truncate large tool result content to prevent context explosion.

    Tool results from MCP can be very large (e.g., search results with hierarchies).
    This function limits the size of 'tool' role messages to prevent exponential
    token growth in the conversation context.

    Args:
        messages: List of message dictionaries
        max_tool_content_chars: Maximum characters to keep in tool message content

    Returns:
        New list with truncated tool messages
    """
    result = []
    for msg in messages:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > max_tool_content_chars:
                # Truncate and add indicator
                truncated_content = (
                    content[:max_tool_content_chars]
                    + f"\n... [truncated {len(content) - max_tool_content_chars} chars]"
                )
                result.append({**msg, "content": truncated_content})
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result


def _serialise_tool_invocations_for_llm_debug(
    tool_invocations: Any,
) -> list[dict[str, Any]]:
    """Persist a stable, compact view of tool invocations in llm_debug_data.

    Keep the common operational fields needed for later diagnosis, but avoid
    storing every raw result payload inline under `tool_invocations`. Search
    tools retain their authoritative outputs separately via `search_evidence`.
    """

    if not isinstance(tool_invocations, list):
        return []

    serialised: list[dict[str, Any]] = []
    for raw_invocation in tool_invocations:
        if not isinstance(raw_invocation, Mapping):
            continue

        tool_name = raw_invocation.get("tool") or raw_invocation.get("method")
        if not isinstance(tool_name, str) or not tool_name.strip():
            tool_name = "unknown"
        else:
            tool_name = tool_name.strip()

        arguments = raw_invocation.get("effective_arguments")
        if arguments in (None, {}):
            arguments = raw_invocation.get("payload")
        if arguments in (None, {}):
            arguments = raw_invocation.get("arguments")

        entry: dict[str, Any] = {
            "tool": tool_name,
            "method": tool_name,
            "arguments": arguments if arguments is not None else {},
        }

        for key in (
            "status",
            "error",
            "error_code",
            "blocked",
            "call_id",
            "result_summary",
            "direct_user_call",
            "duration_ms",
            "ok",
            "auto_retry",
            "knowledge_interaction",
            "write_policy_risk_class",
            "write_policy_outcome",
            "write_policy_decision_basis",
            "write_policy_effective_mutation_authority",
            "write_policy_authority_sources",
        ):
            if key in raw_invocation:
                entry[key] = raw_invocation.get(key)

        serialised.append(entry)

    return serialised


def _serialise_tool_invocations_for_turn_execution_record(
    tool_invocations: Any,
) -> list[dict[str, Any]]:
    """Persist bounded payload evidence needed by effect/read-back validators."""

    if not isinstance(tool_invocations, list):
        return []

    serialised: list[dict[str, Any]] = []
    for raw_invocation in tool_invocations:
        if not isinstance(raw_invocation, Mapping):
            continue

        tool_name = raw_invocation.get("tool") or raw_invocation.get("method")
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
        tool_name = tool_name.strip()

        entry: dict[str, Any] = {
            "tool": tool_name,
            "method": tool_name,
        }
        for key in (
            "status",
            "error",
            "error_code",
            "blocked",
            "call_id",
            "result_summary",
            "duration_ms",
            "workflow_step_evidence",
            "workflow_action_id",
            "workflow_id",
            "workflow_state_id",
        ):
            if key in raw_invocation:
                entry[key] = raw_invocation.get(key)

        for key in (
            "effective_payload",
            "payload",
            "effective_arguments",
            "arguments",
        ):
            value = raw_invocation.get(key)
            if isinstance(value, Mapping):
                entry[key] = _sanitise_diagnostic_export_payload(value)

        serialised.append(entry)

    return serialised


def _reconstruct_tool_messages_from_invocations(
    tool_invocations: Any,
    *,
    orchestrator: Any = None,
) -> list[dict[str, str]]:
    """Best-effort presenter fallback when ``extra_messages`` are missing.

    The authoritative route path is still ``OrchestratorResult.extra_messages``.
    When that list is unexpectedly empty but executed invocations are present,
    rebuild tool-style messages so presenter backfill can remain grounded in the
    actual tool outcomes rather than synthesising a false no-tool narrative.
    """

    if not isinstance(tool_invocations, list):
        return []

    formatter = getattr(orchestrator, "_format_tool_result", None)
    reconstructed: list[dict[str, str]] = []
    for raw_invocation in tool_invocations:
        if not isinstance(raw_invocation, Mapping):
            continue

        tool_name = raw_invocation.get("tool") or raw_invocation.get("method")
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue

        raw_status = raw_invocation.get("status")
        error_text = raw_invocation.get("error")
        status = (
            "error"
            if isinstance(raw_status, str) and raw_status.strip().lower() == "error"
            else "ok"
        )
        payload = raw_invocation.get("effective_payload")
        if payload is None:
            payload = raw_invocation.get("result")
        duration_ms = raw_invocation.get("duration_ms")
        if not isinstance(duration_ms, (int, float)):
            duration_ms = None

        if callable(formatter):
            try:
                content = formatter(
                    tool_name.strip(),
                    payload,
                    duration_ms,
                    status,
                    error_text if isinstance(error_text, str) else None,
                )
            except Exception:
                content = None
            if isinstance(content, str) and content.strip():
                reconstructed.append({"role": "tool", "content": content})
                continue

        fallback_payload: dict[str, Any] = {
            "tool": tool_name.strip(),
            "status": status,
            "duration_ms": duration_ms,
        }
        if status == "ok":
            fallback_payload["payload"] = payload
        if isinstance(error_text, str) and error_text.strip():
            fallback_payload["error"] = error_text.strip()
        reconstructed.append(
            {"role": "tool", "content": json.dumps(fallback_payload, default=str)}
        )

    return reconstructed


def _truncate_debug_payload(raw: str | None, max_chars: int = 4000) -> str | None:
    """Limit rejected tool payloads before surfacing them in LLM debug info."""
    if raw is None:
        return None

    payload = raw if isinstance(raw, str) else str(raw)
    if len(payload) <= max_chars:
        return payload

    return payload[:max_chars] + f"\n... [truncated {len(payload) - max_chars} chars]"


def _limit_context_size(context: list[dict], max_messages: int = 20) -> list[dict]:
    """
    Keep only the most recent messages in context to prevent unbounded growth.

    Always preserves the system message (if present at index 0) and keeps the
    most recent user/assistant exchanges.

    Args:
        context: List of message dictionaries
        max_messages: Maximum number of messages to keep (excluding system message)

    Returns:
        Trimmed context list
    """
    if len(context) <= max_messages:
        return context

    # Check if first message is system message
    if context and context[0].get("role") == "system":
        system_msg = [context[0]]
        recent_msgs = context[-(max_messages - 1) :]  # Keep room for system message
        return system_msg + recent_msgs
    else:
        return context[-max_messages:]


def _calculate_context_stats(messages: list[dict]) -> dict:
    """
    Calculate statistics about message context for debugging.

    Args:
        messages: List of message dictionaries

    Returns:
        Dictionary with context statistics
    """
    stats = {
        "total_messages": len(messages),
        "by_role": {},
        "total_chars": 0,
        "largest_message": {"role": None, "chars": 0},
    }

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        content_len = len(str(content))

        # Count by role
        stats["by_role"][role] = stats["by_role"].get(role, 0) + 1

        # Total characters
        stats["total_chars"] += content_len

        # Track largest message
        if content_len > stats["largest_message"]["chars"]:
            stats["largest_message"] = {"role": role, "chars": content_len}

    return stats


def _calculate_tool_stats(tool_messages: list[dict]) -> dict:
    """
    Calculate statistics about MCP tool results.

    Args:
        tool_messages: List of tool message dictionaries

    Returns:
        Dictionary with tool result statistics
    """
    stats = {
        "tool_count": len(tool_messages),
        "total_chars": 0,
        "truncated_count": 0,
        "tools": [],
    }

    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue

        content = msg.get("content", "")
        content_str = str(content)
        content_len = len(content_str)

        stats["total_chars"] += content_len

        # Check if truncated
        was_truncated = "[truncated" in content_str
        if was_truncated:
            stats["truncated_count"] += 1

        # Try to parse tool name from content
        tool_name = "unknown"
        try:
            import json

            parsed = json.loads(
                content_str.split("[truncated")[0] if was_truncated else content_str
            )
            if isinstance(parsed, dict):
                tool_name = parsed.get("tool", "unknown")
        except Exception:
            pass

        stats["tools"].append(
            {"name": tool_name, "chars": content_len, "truncated": was_truncated}
        )

    return stats


def _derive_llm_debug_warnings(debug_info: dict) -> list[str]:
    """
    Derive warnings from LLM debug information.

    Mirrors the frontend deriveLlmDebugWarnings logic to ensure backend
    warnings are persisted in the JSON structure.

    Args:
        debug_info: The llm_debug_info dictionary

    Returns:
        List of warning strings
    """
    warnings = []

    if not debug_info or not isinstance(debug_info, dict):
        return warnings

    # Check for backend errors
    if isinstance(debug_info.get("error"), str) and debug_info.get("error", "").strip():
        warnings.append(f"Backend error: {debug_info['error'].strip()}")

    # Check auxiliary LLM calls for warnings
    aux_calls = debug_info.get("aux_llm_calls", [])
    if isinstance(aux_calls, list):
        for call in aux_calls:
            if not isinstance(call, dict):
                continue

            call_type = call.get("type", "")

            # Check missing tool-call classifier warnings
            if call_type == "missing_tool_call_classifier":
                injection_mode = call.get("prompt_injection_mode", "")
                if injection_mode == "append":
                    warnings.append(
                        "Missing tool-call detector prompt did not include `{response}` placeholder; response was appended."
                    )

                verdict = str(call.get("response_preview", "")).strip().lower()
                if verdict and not verdict.startswith(("yes", "no")):
                    warnings.append(
                        "Missing tool-call classifier returned an unexpected verdict (not yes/no)."
                    )

                model_raw = call.get("model_raw", "")
                model_resolved = call.get("model_resolved", "")
                if (
                    isinstance(model_raw, str)
                    and model_raw.startswith("#V#")
                    and not model_resolved
                ):
                    warnings.append(
                        "Missing tool-call classifier model could not be resolved from ontology ID."
                    )

            # Check for call-level errors
            if isinstance(call.get("error"), str) and call.get("error", "").strip():
                warnings.append(call["error"].strip())

    # Tool invocation failures / parse errors
    tool_invocations = debug_info.get("tool_invocations", [])
    if isinstance(tool_invocations, list):
        for inv in tool_invocations:
            if not isinstance(inv, dict):
                continue
            method = inv.get("method")
            if not isinstance(method, str):
                method = (
                    inv.get("tool") if isinstance(inv.get("tool"), str) else "unknown"
                )
            error = inv.get("error")
            error_text = error.strip() if isinstance(error, str) else ""
            if not error_text:
                continue

            # Surface parse errors directly so it is obvious tools were not executed.
            if method == "__tool_call_parse_error__":
                warnings.append(error_text)
            else:
                warnings.append(f"Tool {method} failed: {error_text}")

    # Max tool invocation cap reached (LLM still wants tools)
    try:
        internal_mcp = debug_info.get("internal_mcp")
        caps = (
            internal_mcp.get("execution_caps")
            if isinstance(internal_mcp, dict)
            else None
        )
        max_invocations = (
            caps.get("max_tool_invocations") if isinstance(caps, dict) else None
        )
        max_invocations = int(max_invocations) if max_invocations is not None else None
    except Exception:
        max_invocations = None

    response_text = debug_info.get("response")
    if (
        isinstance(response_text, str)
        and isinstance(max_invocations, int)
        and max_invocations > 0
        and isinstance(tool_invocations, list)
        and len(tool_invocations) >= max_invocations
    ):
        trimmed = response_text.strip()
        looks_like_tool_call = (
            (trimmed.startswith("{") or trimmed.startswith("["))
            and '"call_tool"' in trimmed
            and '"tool"' in trimmed
        )
        if looks_like_tool_call:
            warnings.append(
                f"Configured tool-invocation cap ({max_invocations}) was reached; later tool-shaped output was not executed."
            )

    # Output/presenter/display health is typed separately so consumers can route
    # it to output surfaces instead of treating it as LLM execution failure.
    warnings.extend(turn_output_health_issue_messages(debug_info))

    # Remove duplicates while preserving order
    seen = set()
    unique_warnings = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique_warnings.append(w)

    return unique_warnings


def _normalise_workflow_routing_payload(
    workflow_routing: Any,
) -> dict[str, Any] | None:
    if workflow_routing is None:
        return None
    if isinstance(workflow_routing, Mapping):
        return {
            str(key): value
            for key, value in workflow_routing.items()
            if isinstance(key, str)
        }
    if is_dataclass(workflow_routing) and not isinstance(workflow_routing, type):
        try:
            payload = asdict(workflow_routing)
            if isinstance(payload, dict):
                return {
                    str(key): value
                    for key, value in payload.items()
                    if isinstance(key, str)
                }
        except Exception:
            return None
    raw_dict = getattr(workflow_routing, "__dict__", None)
    if isinstance(raw_dict, dict):
        return {
            str(key): value for key, value in raw_dict.items() if isinstance(key, str)
        }
    return None


def _latest_turn_completion_gate(aux_calls: Any) -> dict[str, Any] | None:
    if not isinstance(aux_calls, (list, tuple)):
        return None

    for entry in reversed(aux_calls):
        if not isinstance(entry, Mapping):
            continue
        call_type = entry.get("type")
        if (
            not isinstance(call_type, str)
            or call_type.strip() != "turn_completion_gate"
        ):
            continue

        decision = _progress_str(entry.get("decision"))
        decision_reason = _progress_str(entry.get("decision_reason"))
        requires_follow_up = bool(entry.get("requires_follow_up", False))
        safe_to_claim_completion = bool(
            entry.get("safe_to_claim_completion", not requires_follow_up)
        )
        blocking_effect_ids_raw = entry.get("blocking_effect_ids")
        blocking_effect_ids: list[str] = []
        if isinstance(blocking_effect_ids_raw, list):
            for item in blocking_effect_ids_raw:
                effect_id = _progress_str(item)
                if effect_id:
                    blocking_effect_ids.append(effect_id)
        blocking_failure_codes_raw = entry.get("blocking_failure_codes")
        blocking_failure_codes: list[str] = []
        if isinstance(blocking_failure_codes_raw, list):
            for item in blocking_failure_codes_raw:
                failure_code = _progress_str(item)
                if failure_code:
                    blocking_failure_codes.append(failure_code)
        unresolved_preconditions_raw = entry.get("unresolved_preconditions")
        unresolved_preconditions = (
            [
                dict(item)
                for item in unresolved_preconditions_raw
                if isinstance(item, Mapping)
            ]
            if isinstance(unresolved_preconditions_raw, list)
            else []
        )
        evidence_payload_raw = entry.get("evidence_payload")
        evidence_payload = (
            dict(evidence_payload_raw)
            if isinstance(evidence_payload_raw, Mapping)
            else None
        )

        return {
            "decision": decision,
            "decision_reason": decision_reason,
            "requires_follow_up": requires_follow_up,
            "safe_to_claim_completion": safe_to_claim_completion,
            "blocking_effect_ids": blocking_effect_ids,
            "blocking_failure_codes": blocking_failure_codes,
            "unresolved_preconditions": unresolved_preconditions,
            "evidence_payload": evidence_payload,
        }

    return None


def _build_terminal_tool_progress_payload(
    *,
    request_id: str,
    aux_calls: Any,
    completion_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    completion_gate = (
        dict(completion_gate)
        if isinstance(completion_gate, Mapping)
        else _latest_turn_completion_gate(aux_calls)
    )
    requires_follow_up = bool(
        completion_gate.get("requires_follow_up", False)
        if isinstance(completion_gate, dict)
        else False
    )
    safe_to_claim_completion = bool(
        completion_gate.get("safe_to_claim_completion", not requires_follow_up)
        if isinstance(completion_gate, dict)
        else True
    )
    progress_success = bool(safe_to_claim_completion and not requires_follow_up)
    terminal_status = "completed" if progress_success else "follow_up_required"
    terminal_label = "Complete" if progress_success else "Follow-up required"

    payload: dict[str, Any] = {
        "status": terminal_status,
        "stage": terminal_status,
        "phase_label": terminal_label,
        "request_id": request_id,
        "success": progress_success,
        "completion_gate_requires_follow_up": requires_follow_up,
        "completion_gate_safe_to_claim_completion": safe_to_claim_completion,
        "orchestrator_status": terminal_status,
    }
    if isinstance(completion_gate, dict):
        payload["completion_gate_decision"] = completion_gate.get("decision")
        payload["completion_gate_decision_reason"] = completion_gate.get(
            "decision_reason"
        )
        blocking_effect_ids = completion_gate.get("blocking_effect_ids")
        if isinstance(blocking_effect_ids, list):
            payload["completion_gate_blocking_effect_ids"] = list(blocking_effect_ids)
    return payload


def _build_request_initialising_tool_progress_payload(
    *,
    request_id: str,
    goal_label: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "thinking",
        "stage": "context_build",
        "phase": "context_build",
        "phase_label": "Initialising request",
        "request_id": request_id,
        "workflow_task": "context_build",
        "subtask": "request setup",
        "result_summary": (
            "Resolving session scope, namespace, and chat-history context before "
            "workflow/tool selection."
        ),
    }
    clean_goal_label = _progress_str(goal_label)
    if clean_goal_label:
        payload["goal_label"] = clean_goal_label
    return payload


def _build_pending_tool_progress_placeholder_payload(
    *,
    request_id: str,
    status_code: int,
) -> dict[str, Any]:
    request_known = int(status_code) != 404
    phase_label = (
        "Awaiting visible progress" if request_known else "Request status unavailable"
    )
    result_summary = (
        "No live progress state is visible yet. The request may still be in early "
        "turn setup, or progress visibility for this session has not caught up yet."
        if request_known
        else "No visible progress record was found for this request in the current "
        "session."
    )
    pending_reason = (
        "no_visible_progress_state" if request_known else "request_progress_not_found"
    )
    return {
        "status": "pending",
        "stage": "context_build",
        "phase": "context_build",
        "phase_label": phase_label,
        "stage_label": "Build context",
        "request_id": request_id,
        "liveness_state": "waiting",
        "liveness_reason": pending_reason,
        "pending_reason": pending_reason,
        "subtask": "progress visibility",
        "result_summary": result_summary,
    }


def _estimate_response_finalising_eta_ms(
    *,
    response_text: str | None,
    tool_message_count: int,
    persist_history: bool,
) -> int:
    """Return a bounded best-effort ETA for post-orchestrator finalisation."""
    eta_ms = 900
    eta_ms += min(900, max(0, int(tool_message_count)) * 120)
    response_chars = len(response_text.strip()) if isinstance(response_text, str) else 0
    eta_ms += min(1400, max(0, response_chars // 12))
    if persist_history:
        eta_ms += 800
    return max(800, min(eta_ms, 12_000))


def _build_response_finalising_tool_progress_payload(
    *,
    request_id: str,
    eta_ms: int,
    response_text: str | None,
    tool_message_count: int,
    persist_history: bool,
) -> dict[str, Any]:
    detail_bits: list[str] = ["assembling the final response payload"]
    if tool_message_count > 0:
        detail_bits.append(
            f"summarising {int(tool_message_count)} tool result"
            f"{'' if int(tool_message_count) == 1 else 's'}"
        )
    if persist_history:
        detail_bits.append("persisting chat history")
    result_summary = "; ".join(detail_bits).strip()
    response_chars = len(response_text.strip()) if isinstance(response_text, str) else 0
    return {
        "status": "phase_transition",
        "stage": "response_finalising",
        "phase": "response_finalising",
        "phase_label": "Finalising response",
        "request_id": request_id,
        "workflow_task": "response_finalising",
        "subtask": "response assembly",
        "result_summary": result_summary,
        "eta_ms": max(0, int(eta_ms)),
        "tool_message_count": max(0, int(tool_message_count)),
        "persist_history": bool(persist_history),
        "response_chars": max(0, response_chars),
    }


def _finalise_llm_debug_info(
    *,
    llm_debug_info: dict[str, Any],
    prompt_text: str | None,
    response_text: str | None,
    session_id: str | None,
    namespace: str | None,
    actor_concept_id: str | None = None,
    user_id: str | None,
    org_id: str | None,
    workflow_discovery: dict[str, Any] | None = None,
    workflow_routing: Any = None,
) -> dict[str, Any]:
    if not isinstance(llm_debug_info, dict):
        return llm_debug_info

    tool_invocations_payload = (
        llm_debug_info.get("tool_invocations")
        if isinstance(llm_debug_info.get("tool_invocations"), list)
        else []
    )
    turn_record_tool_invocations_payload = (
        llm_debug_info.get("turn_execution_record_tool_invocations")
        if isinstance(
            llm_debug_info.get("turn_execution_record_tool_invocations"), list
        )
        else tool_invocations_payload
    )
    search_evidence_payload = (
        llm_debug_info.get("search_evidence")
        if isinstance(llm_debug_info.get("search_evidence"), list)
        else None
    )
    if search_evidence_payload is None:
        search_evidence_payload = build_search_tool_evidence(
            turn_record_tool_invocations_payload
        )
        if search_evidence_payload:
            llm_debug_info["search_evidence"] = search_evidence_payload

    code_version_details = get_runtime_code_version_info()
    llm_debug_info["code_version"] = _progress_str(code_version_details.get("version"))
    llm_debug_info["code_version_details"] = code_version_details
    diagnostics_payload = llm_debug_info.get("turn_execution_diagnostics")
    if isinstance(diagnostics_payload, dict):
        diagnostics_payload["code_version"] = _progress_str(
            code_version_details.get("version")
        )
        diagnostics_payload["code_version_details"] = code_version_details

    workflow_discovery_payload = workflow_discovery
    if workflow_discovery_payload is None:
        raw_discovery = llm_debug_info.get("workflow_discovery")
        if isinstance(raw_discovery, dict):
            workflow_discovery_payload = dict(raw_discovery)

    workflow_routing_payload = _normalise_workflow_routing_payload(workflow_routing)
    if workflow_routing_payload is None:
        raw_routing = llm_debug_info.get("workflow_routing")
        workflow_routing_payload = _normalise_workflow_routing_payload(raw_routing)

    resolved_actor_concept_id = actor_concept_id
    if (
        not isinstance(resolved_actor_concept_id, str)
        or not resolved_actor_concept_id.strip()
    ):
        raw_actor_concept_id = llm_debug_info.get("actor_concept_id")
        if isinstance(raw_actor_concept_id, str) and raw_actor_concept_id.strip():
            resolved_actor_concept_id = raw_actor_concept_id.strip()
        elif isinstance(namespace, str) and namespace.strip():
            resolved_actor_concept_id = namespace.strip()
        else:
            resolved_actor_concept_id = None
    if isinstance(resolved_actor_concept_id, str) and resolved_actor_concept_id.strip():
        llm_debug_info["actor_concept_id"] = resolved_actor_concept_id

    try:
        turn_execution_record = build_turn_execution_record(
            request_id=llm_debug_info.get("request_id"),
            session_id=session_id,
            namespace=namespace,
            actor_concept_id=resolved_actor_concept_id,
            user_id=user_id,
            org_id=org_id,
            prompt_text=prompt_text,
            response_text=(
                response_text
                if isinstance(response_text, str)
                else llm_debug_info.get("response")
            ),
            interaction_timestamp_utc=llm_debug_info.get("interaction_timestamp_utc"),
            workflow_discovery=workflow_discovery_payload,
            workflow_routing=workflow_routing_payload,
            tool_invocations=turn_record_tool_invocations_payload,
            search_evidence=search_evidence_payload,
            turn_execution_diagnostics=(
                llm_debug_info.get("turn_execution_diagnostics")
                if isinstance(llm_debug_info.get("turn_execution_diagnostics"), dict)
                else None
            ),
            aux_llm_calls=(
                llm_debug_info.get("aux_llm_calls")
                if isinstance(llm_debug_info.get("aux_llm_calls"), list)
                else []
            ),
            llm_calls=(
                llm_debug_info.get("llm_interaction", {}).get("calls")
                if isinstance(llm_debug_info.get("llm_interaction"), dict)
                and isinstance(
                    llm_debug_info.get("llm_interaction", {}).get("calls"), list
                )
                else (
                    llm_debug_info.get("llm_calls")
                    if isinstance(llm_debug_info.get("llm_calls"), list)
                    else []
                )
            ),
            selected_workflow_trace=(
                llm_debug_info.get("selected_workflow_trace")
                if isinstance(llm_debug_info.get("selected_workflow_trace"), dict)
                else None
            ),
            critic_verdict=(
                llm_debug_info.get("critic_verdict")
                if isinstance(llm_debug_info.get("critic_verdict"), dict)
                else None
            ),
            completion_gate_verdict=(
                llm_debug_info.get("completion_gate_verdict")
                if isinstance(llm_debug_info.get("completion_gate_verdict"), dict)
                else None
            ),
            completion_report=(
                llm_debug_info.get("completion_report")
                if isinstance(llm_debug_info.get("completion_report"), dict)
                else None
            ),
            required_tool_obligation_ledger=(
                llm_debug_info.get("required_tool_obligation_ledger")
                if isinstance(
                    llm_debug_info.get("required_tool_obligation_ledger"), dict
                )
                else None
            ),
        )
        llm_debug_info["turn_execution_record"] = turn_execution_record
        routing_diagnostics = turn_execution_record.get("workflow_routing_diagnostics")
        if isinstance(routing_diagnostics, Mapping):
            llm_debug_info["workflow_routing_diagnostics"] = dict(routing_diagnostics)
            diagnostics_payload = llm_debug_info.get("turn_execution_diagnostics")
            if isinstance(diagnostics_payload, dict):
                diagnostics_payload["workflow_routing_diagnostics"] = dict(
                    routing_diagnostics
                )
        completion_gate = turn_execution_record.get("completion_gate")
        if isinstance(completion_gate, Mapping):
            raw_completion_gate_verdict = llm_debug_info.get("completion_gate_verdict")
            if isinstance(raw_completion_gate_verdict, Mapping):
                llm_debug_info["raw_completion_gate_verdict"] = dict(
                    raw_completion_gate_verdict
                )
            llm_debug_info["completion_gate"] = dict(completion_gate)
            llm_debug_info["completion_gate_verdict"] = dict(completion_gate)
            diagnostics_payload = llm_debug_info.get("turn_execution_diagnostics")
            if isinstance(diagnostics_payload, dict):
                diagnostics_payload["completion_gate"] = dict(completion_gate)
    except Exception as exc:
        try:
            current_app.logger.warning(
                "[turn_execution_record] Failed to build turn execution record: %s",
                exc,
            )
        except Exception:
            pass

    llm_debug_info["turn_output_health"] = build_turn_output_health(llm_debug_info)
    llm_debug_info["warnings"] = _derive_llm_debug_warnings(llm_debug_info)
    return llm_debug_info


_FENCED_CODE_BLOCK_PATTERN = re.compile(
    r"```[\w+\-]*\n.*?(?:```|$)",
    flags=re.DOTALL,
)


def _strip_fenced_code_blocks(text: str) -> str:
    if not isinstance(text, str) or not text:
        return ""
    return _FENCED_CODE_BLOCK_PATTERN.sub("", text)


def _mask_fenced_code_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Replace fenced code blocks with placeholders for safe tag matching.

    This allows presenter-tag extraction to ignore tags *inside* fenced code
    while still restoring fenced content that belongs inside a valid <screen>
    block.
    """

    if not isinstance(text, str) or not text:
        return "", {}

    replacements: dict[str, str] = {}
    counter = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"__VON_FENCED_BLOCK_{counter}__"
        replacements[token] = match.group(0)
        counter += 1
        return token

    masked = _FENCED_CODE_BLOCK_PATTERN.sub(_replace, text)
    return masked, replacements


def _restore_masked_fenced_code_blocks(
    text: str,
    replacements: dict[str, str],
) -> str:
    if not isinstance(text, str) or not text:
        return ""
    restored = text
    for token, block in replacements.items():
        restored = restored.replace(token, block)
    return restored


def _extract_tagged_block(text: str, tag: str) -> str | None:
    """Extract a presenter tag value while preserving fenced code in content."""

    if not isinstance(text, str) or not text:
        return None
    if not isinstance(tag, str) or not tag:
        return None

    searchable, replacements = _mask_fenced_code_blocks(text)
    pattern = rf"<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>"
    match = re.search(pattern, searchable, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    if not isinstance(value, str):
        return None
    value = _restore_masked_fenced_code_blocks(value, replacements).strip()
    return value if value else None


def _extract_presenter_channels(text: str) -> dict[str, object] | None:
    """Extract presenter-style output blocks.

    Expected format (v1):
      <spoken>...talk track...</spoken>
      <screen>...what to display...</screen>

    Returns None when no tags are present.
    """

    if not isinstance(text, str) or not text:
        return None

    spoken = _extract_tagged_block(text, "spoken")
    screen = _extract_tagged_block(text, "screen")

    if spoken is None and screen is None:
        return None

    # Defaults:
    # - If only <spoken> is provided, display it on-screen too (otherwise we'd render the raw tags).
    # - If only <screen> is provided, do NOT fabricate spoken from screen; let the client fall back.
    screen_text = screen if screen is not None else spoken
    spoken_text = spoken

    if screen_text is None:
        screen_text = text.strip()

    return {
        "format": "tagged_blocks_v1",
        "extracted": True,
        "screen": screen_text,
        "spoken": spoken_text,
    }


def _presenter_tag_present(text: str, tag: str) -> bool:
    return _extract_tagged_block(text, tag) is not None


def _build_presenter_screen_from_tool_messages(
    tool_messages: list[dict],
) -> str | None:
    if not tool_messages:
        return None

    import json

    entries: list[str] = []
    index = 0
    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        index += 1
        parsed = None
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None

        if isinstance(parsed, dict):
            tool_name = parsed.get("tool") or parsed.get("method") or "tool"
            status = parsed.get("status")
            duration_ms = parsed.get("duration_ms")
            error = parsed.get("error")
            payload = parsed.get("payload")

            lines = [f"{index}. {tool_name}"]
            if status:
                lines.append(f"Status: {status}")
            if duration_ms is not None:
                lines.append(f"Duration: {duration_ms} ms")
            if error:
                lines.append(f"Error: {error}")
            if payload is not None:
                try:
                    payload_text = json.dumps(
                        payload,
                        indent=2,
                        sort_keys=True,
                        ensure_ascii=True,
                    )
                except Exception:
                    payload_text = str(payload)
                lines.append("Payload:")
                lines.append("```json")
                lines.append(payload_text)
                lines.append("```")
            entries.append("\n".join(lines))
        else:
            raw = content.strip()
            entries.append(f"{index}. Tool result\n```\n{raw}\n```")

    if not entries:
        return None

    return "Tool results:\n\n" + "\n\n".join(entries)


def _extract_required_screen_json_fence(prompt_text: str | None) -> str | None:
    """Extract a required JSON fence requested by the user prompt.

    Handles both well-formed fenced blocks and partially rendered prompts where
    the closing code fence may have been stripped by transport/presenter layers.
    """

    if not isinstance(prompt_text, str) or not prompt_text.strip():
        return None

    text = prompt_text
    lowered = text.lower()
    if "fenced block" not in lowered and "```json" not in lowered:
        return None

    fenced_match = re.search(
        r"```json\s*\n(?P<body>[\s\S]*?)\n```",
        text,
        flags=re.IGNORECASE,
    )
    if fenced_match:
        body = str(fenced_match.group("body") or "").strip()
        if body:
            return f"```json\n{body}\n```"

    sentinel_json_match = re.search(
        r"\{\s*\"sentinel\"\s*:\s*\"[^\"]+\"\s*,\s*\"check\"\s*:\s*\"[^\"]+\"\s*\}",
        text,
        flags=re.IGNORECASE,
    )
    if sentinel_json_match:
        body = str(sentinel_json_match.group(0) or "").strip()
        if body:
            return f"```json\n{body}\n```"

    return None


def _extract_screen_table_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract table specs from renderer plan diagnostics.

    Supports both prebuilt payloads and record-set definitions that are
    converted through the canonical table payload builder.
    """
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("table", True)
    ):
        return []

    table_elements: list[dict[str, Any]] = []

    def _append_table_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    table_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            table_elements.append(dict(raw_value))

    _append_table_specs(render_plan.get("screen_table_elements"))
    _append_table_specs(render_plan.get("screen_table_payloads"))

    record_sets_raw = render_plan.get("screen_table_record_sets")
    record_sets: list[dict[str, Any]] = []
    if isinstance(record_sets_raw, list):
        for item in record_sets_raw:
            if isinstance(item, dict):
                record_sets.append(item)
    elif isinstance(record_sets_raw, dict):
        record_sets.append(record_sets_raw)

    single_record_set = render_plan.get("screen_table_record_set")
    if isinstance(single_record_set, dict):
        record_sets.append(single_record_set)

    for index, record_set in enumerate(record_sets, start=1):
        records = record_set.get("records")
        columns = record_set.get("columns")
        if not isinstance(records, list) or not isinstance(columns, list):
            continue

        page_size_raw = record_set.get("page_size")
        page_size = (
            int(page_size_raw)
            if isinstance(page_size_raw, int) and page_size_raw > 0
            else None
        )
        record_set_title = (
            str(record_set.get("title"))
            if isinstance(record_set.get("title"), str)
            else (
                str(record_set.get("label"))
                if isinstance(record_set.get("label"), str)
                else None
            )
        )

        filters = record_set.get("filters")
        payload = build_canonical_table_payload_from_records(
            records=records,
            columns=columns,
            row_id_field=(
                str(record_set.get("row_id_field"))
                if isinstance(record_set.get("row_id_field"), str)
                else None
            ),
            row_provenance_field=(
                str(record_set.get("row_provenance_field"))
                if isinstance(record_set.get("row_provenance_field"), str)
                else None
            ),
            default_sort_column_id=(
                str(record_set.get("default_sort_column_id"))
                if isinstance(record_set.get("default_sort_column_id"), str)
                else None
            ),
            default_sort_direction=(
                str(record_set.get("default_sort_direction"))
                if isinstance(record_set.get("default_sort_direction"), str)
                else "asc"
            ),
            filters=filters if isinstance(filters, list) else None,
            pagination_enabled=bool(record_set.get("pagination_enabled", True)),
            page_size=page_size,
            title=record_set_title,
        )

        spec: dict[str, Any] = {"payload": payload}
        if isinstance(record_set.get("element_id"), str):
            spec["element_id"] = str(record_set["element_id"])
        if isinstance(record_set.get("order"), int):
            spec["order"] = int(record_set["order"])
        if isinstance(record_set.get("intent"), str):
            spec["intent"] = str(record_set["intent"])
        if isinstance(record_set.get("constraints"), dict):
            spec["constraints"] = dict(record_set["constraints"])
        provenance = record_set.get("provenance")
        if isinstance(provenance, dict):
            spec["provenance"] = dict(provenance)
        else:
            spec["provenance"] = {
                "source": "render_plan_table_record_set",
                "record_set_index": index,
            }
        table_elements.append(spec)

    return table_elements


def _extract_screen_workflow_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract workflow specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("workflow_view", True)
    ):
        return []

    workflow_elements: list[dict[str, Any]] = []

    def _append_workflow_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    workflow_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            workflow_elements.append(dict(raw_value))

    _append_workflow_specs(render_plan.get("screen_workflow_elements"))
    _append_workflow_specs(render_plan.get("screen_workflow_payloads"))

    single_workflow = render_plan.get("screen_workflow_element")
    if isinstance(single_workflow, dict):
        workflow_elements.append(dict(single_workflow))

    return workflow_elements


def _extract_screen_task_view_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract task_view specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("task_view", True)
    ):
        return []

    task_view_elements: list[dict[str, Any]] = []

    def _append_task_view_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    task_view_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            task_view_elements.append(dict(raw_value))

    _append_task_view_specs(render_plan.get("screen_task_view_elements"))
    _append_task_view_specs(render_plan.get("screen_task_view_payloads"))

    single_task_view = render_plan.get("screen_task_view_element")
    if isinstance(single_task_view, dict):
        task_view_elements.append(dict(single_task_view))

    return task_view_elements


def _extract_screen_calendar_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract calendar specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("calendar_view", True)
    ):
        return []

    calendar_elements: list[dict[str, Any]] = []

    def _append_calendar_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    calendar_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            calendar_elements.append(dict(raw_value))

    _append_calendar_specs(render_plan.get("screen_calendar_elements"))
    _append_calendar_specs(render_plan.get("screen_calendar_payloads"))

    single_calendar = render_plan.get("screen_calendar_element")
    if isinstance(single_calendar, dict):
        calendar_elements.append(dict(single_calendar))

    return calendar_elements


def _extract_screen_chart_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract chart_view specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("chart_view", True)
    ):
        return []

    chart_elements: list[dict[str, Any]] = []

    def _append_chart_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    chart_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            chart_elements.append(dict(raw_value))

    _append_chart_specs(render_plan.get("screen_chart_elements"))
    _append_chart_specs(render_plan.get("screen_chart_payloads"))

    single_chart = render_plan.get("screen_chart_element")
    if isinstance(single_chart, dict):
        chart_elements.append(dict(single_chart))

    return chart_elements


def _extract_screen_document_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract document_view specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("document_view", True)
    ):
        return []

    document_elements: list[dict[str, Any]] = []

    def _append_document_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    document_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            document_elements.append(dict(raw_value))

    _append_document_specs(render_plan.get("screen_document_elements"))
    _append_document_specs(render_plan.get("screen_document_payloads"))

    single_document = render_plan.get("screen_document_element")
    if isinstance(single_document, dict):
        document_elements.append(dict(single_document))

    return document_elements


def _extract_screen_location_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract location_view specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("location_view", True)
    ):
        return []

    location_elements: list[dict[str, Any]] = []

    def _append_location_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    location_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            location_elements.append(dict(raw_value))

    _append_location_specs(render_plan.get("screen_location_elements"))
    _append_location_specs(render_plan.get("screen_location_payloads"))

    single_location = render_plan.get("screen_location_element")
    if isinstance(single_location, dict):
        location_elements.append(dict(single_location))

    return location_elements


def _extract_screen_kanban_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract kanban_view specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("kanban_view", True)
    ):
        return []

    kanban_elements: list[dict[str, Any]] = []

    def _append_kanban_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    kanban_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            kanban_elements.append(dict(raw_value))

    _append_kanban_specs(render_plan.get("screen_kanban_elements"))
    _append_kanban_specs(render_plan.get("screen_kanban_payloads"))

    single_kanban = render_plan.get("screen_kanban_element")
    if isinstance(single_kanban, dict):
        kanban_elements.append(dict(single_kanban))

    return kanban_elements


def _extract_screen_timeline_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract timeline specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("timeline", True)
    ):
        return []

    timeline_elements: list[dict[str, Any]] = []

    def _append_timeline_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    timeline_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            timeline_elements.append(dict(raw_value))

    _append_timeline_specs(render_plan.get("screen_timeline_elements"))
    _append_timeline_specs(render_plan.get("screen_timeline_payloads"))

    single_timeline = render_plan.get("screen_timeline_element")
    if isinstance(single_timeline, dict):
        timeline_elements.append(dict(single_timeline))

    return timeline_elements


def _extract_screen_relation_graph_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract relation graph specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("relation_graph_view", True)
    ):
        return []

    relation_graph_elements: list[dict[str, Any]] = []

    def _append_relation_graph_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    relation_graph_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            relation_graph_elements.append(dict(raw_value))

    _append_relation_graph_specs(render_plan.get("screen_relation_graph_elements"))
    _append_relation_graph_specs(render_plan.get("screen_relation_graph_payloads"))

    single_relation_graph = render_plan.get("screen_relation_graph_element")
    if isinstance(single_relation_graph, dict):
        relation_graph_elements.append(dict(single_relation_graph))

    return relation_graph_elements


def _extract_screen_hierarchy_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract hierarchy specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("hierarchy_view", True)
    ):
        return []

    hierarchy_elements: list[dict[str, Any]] = []

    def _append_hierarchy_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    hierarchy_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            hierarchy_elements.append(dict(raw_value))

    _append_hierarchy_specs(render_plan.get("screen_hierarchy_elements"))
    _append_hierarchy_specs(render_plan.get("screen_hierarchy_payloads"))

    single_hierarchy = render_plan.get("screen_hierarchy_element")
    if isinstance(single_hierarchy, dict):
        hierarchy_elements.append(dict(single_hierarchy))

    return hierarchy_elements


def _extract_screen_relation_truth_state_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract relation truth-state specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("relation_truth_state", True)
    ):
        return []

    relation_truth_state_elements: list[dict[str, Any]] = []

    def _append_relation_truth_state_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    relation_truth_state_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            relation_truth_state_elements.append(dict(raw_value))

    _append_relation_truth_state_specs(
        render_plan.get("screen_relation_truth_state_elements")
    )
    _append_relation_truth_state_specs(
        render_plan.get("screen_relation_truth_state_payloads")
    )

    single_relation_truth_state = render_plan.get("screen_relation_truth_state_element")
    if isinstance(single_relation_truth_state, dict):
        relation_truth_state_elements.append(dict(single_relation_truth_state))

    return relation_truth_state_elements


def _extract_screen_element_targets_from_render_plan(
    render_plan: dict[str, Any] | None,
) -> dict[str, bool]:
    """Resolve render-plan screen element targets with legacy-compatible defaults."""
    default_targets: dict[str, bool] = {
        "table": True,
        "workflow_view": True,
        "task_view": True,
        "calendar_view": True,
        "chart_view": True,
        "location_view": True,
        "document_view": True,
        "kanban_view": True,
        "timeline": True,
        "hierarchy_view": True,
        "relation_graph_view": True,
        "relation_truth_state": True,
    }
    if not isinstance(render_plan, dict):
        return default_targets

    raw_targets = render_plan.get("screen_element_targets")
    if not isinstance(raw_targets, dict):
        return default_targets

    return {
        "table": bool(raw_targets.get("table", True)),
        "workflow_view": bool(raw_targets.get("workflow_view", True)),
        "task_view": bool(raw_targets.get("task_view", True)),
        "calendar_view": bool(raw_targets.get("calendar_view", True)),
        "chart_view": bool(raw_targets.get("chart_view", True)),
        "location_view": bool(raw_targets.get("location_view", True)),
        "document_view": bool(raw_targets.get("document_view", True)),
        "kanban_view": bool(raw_targets.get("kanban_view", True)),
        "timeline": bool(raw_targets.get("timeline", True)),
        "hierarchy_view": bool(raw_targets.get("hierarchy_view", True)),
        "relation_graph_view": bool(raw_targets.get("relation_graph_view", True)),
        "relation_truth_state": bool(raw_targets.get("relation_truth_state", True)),
    }


def _extract_screen_element_reason_codes_from_render_plan(
    render_plan: dict[str, Any] | None,
) -> list[str]:
    """Return renderer mapping diagnostics for display element contract reason codes."""
    if not isinstance(render_plan, dict):
        return []

    raw_reason_codes = render_plan.get("screen_element_reason_codes")
    if not isinstance(raw_reason_codes, list):
        return []

    reason_codes: list[str] = []
    for raw_reason_code in raw_reason_codes:
        if not isinstance(raw_reason_code, str):
            continue
        reason_code = raw_reason_code.strip()
        if not reason_code or reason_code in reason_codes:
            continue
        reason_codes.append(reason_code)
    return reason_codes


def _ensure_required_screen_json_fence(
    screen_text: str | None,
    required_fence: str | None,
) -> str | None:
    """Append a required JSON fence when the screen output is missing it."""

    if not isinstance(required_fence, str) or not required_fence.strip():
        return screen_text

    required_value = required_fence.strip()
    base = screen_text.strip() if isinstance(screen_text, str) else ""
    if required_value in base:
        return base
    if not base:
        return required_value
    return f"{base}\n\n{required_value}"


def _extract_screen_only(text: str) -> str | None:
    return _extract_tagged_block(text, "screen")


def _looks_like_internal_status_diagnostic(value: str | None) -> bool:
    lowered = (value or "").strip().lower()
    if not lowered:
        return False

    diagnostic_markers = (
        "execution status:",
        "blocking effect ids:",
        "unresolved preconditions:",
        "failure codes:",
        "tool activity diagnostics:",
        "operational summary:",
    )
    marker_count = sum(1 for marker in diagnostic_markers if marker in lowered)
    if lowered.startswith("execution status:") or marker_count >= 2:
        return True

    if lowered.startswith("i do not yet have a complete workflow-backed answer"):
        return True

    if lowered.startswith("i ran tools for this request"):
        return True

    if "required workflow_execute-based" in lowered:
        return True

    if lowered.startswith("workflow ") and " (state:" in lowered:
        return True

    return False


def _append_presenter_detector_event(
    auxiliary_llm_calls: list[dict[str, Any]] | None,
    *,
    function_name: str,
    detector: str,
    context: str,
    reason_code: str,
    preview: str | None = None,
) -> None:
    """Record detector-level presenter telemetry when Python pattern checks branch."""

    if not isinstance(auxiliary_llm_calls, list):
        return

    payload: dict[str, Any] = {
        "type": "presenter_detector",
        "stage": "screen_backfill",
        "detector": detector,
        "context": context,
        "matched": True,
    }
    if isinstance(preview, str) and preview.strip():
        payload["preview"] = preview.strip()[:240]

    try:
        auxiliary_llm_calls.append(
            annotate_python_decision_event(
                payload,
                stage="screen_backfill",
                component="presenter_routes",
                function=function_name,
                decision_class="presenter_detector",
                decision_source="structural_pattern_detection",
                changed_outcome=True,
                reason_code=reason_code,
                possible_inappropriate_python_code_use=True,
            )
        )
    except Exception:
        return


def _extract_created_concept_labels_from_payload(
    payload: dict[str, Any], *, max_items: int = 3
) -> list[str]:
    """Extract stable labels only for concepts explicitly reported as created."""
    if not isinstance(payload, dict):
        return []

    labels: list[str] = []
    seen: set[str] = set()
    labelled_created_ids: set[str] = set()

    def _append_label(label: str | None) -> None:
        if not isinstance(label, str):
            return
        cleaned = label.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        labels.append(cleaned)

    created_ids = payload.get("created_concept_ids")
    created_id_set: set[str] = set()
    created_id_list: list[str] = []
    if isinstance(created_ids, list):
        for concept_id in created_ids:
            if isinstance(concept_id, str) and concept_id.strip():
                clean_id = concept_id.strip()
                if clean_id not in created_id_set:
                    created_id_set.add(clean_id)
                    created_id_list.append(clean_id)

    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict) or not bool(item.get("success")):
                continue
            if (
                item.get("duplicate_prevented") is True
                or item.get("error_code") == "already_exists"
            ):
                continue
            item_mutation_kind = item.get("mutation_kind")
            item_created = (
                item.get("created") is True or item.get("was_created") is True
            )
            if isinstance(item_mutation_kind, str):
                item_created = item_created or item_mutation_kind.strip().lower() in {
                    "created",
                    "materialised",
                    "materialized",
                }
            requested_name_raw = (
                item.get("requested_name") or item.get("input_name") or item.get("name")
            )
            requested_name = (
                requested_name_raw.strip()
                if isinstance(requested_name_raw, str) and requested_name_raw.strip()
                else None
            )

            concept_id_value: str | None = None
            for key in ("concept_id", "canonical_concept_id"):
                raw = item.get(key)
                if isinstance(raw, str) and raw.strip():
                    concept_id_value = raw.strip()
                    break
            if concept_id_value is None:
                nested_concept = item.get("concept")
                if isinstance(nested_concept, dict):
                    nested_id = nested_concept.get("concept_id")
                    if isinstance(nested_id, str) and nested_id.strip():
                        concept_id_value = nested_id.strip()
            if (
                not item_created
                and not created_id_set
                and requested_name
                and concept_id_value
            ):
                item_created = True
            if not created_id_set and not item_created:
                continue

            if (
                created_id_set
                and isinstance(concept_id_value, str)
                and concept_id_value not in created_id_set
            ):
                continue

            if requested_name and concept_id_value:
                _append_label(f"{requested_name} ({concept_id_value})")
                labelled_created_ids.add(concept_id_value)
            elif concept_id_value:
                _append_label(concept_id_value)
                labelled_created_ids.add(concept_id_value)
            elif requested_name:
                _append_label(requested_name)

    for concept_id in created_id_list:
        if concept_id not in labelled_created_ids:
            _append_label(concept_id)

    return labels[: max(1, max_items)]


def _iter_presenter_nested_mappings(
    value: Any,
    *,
    max_depth: int = 7,
    max_nodes: int = 240,
) -> Iterator[Mapping[str, Any]]:
    """Yield bounded nested mapping evidence from tool payloads."""

    import json

    stack: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while stack and visited < max_nodes:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(current, Mapping):
            visited += 1
            yield current

            preview = current.get("_preview")
            if isinstance(preview, str) and preview.strip().startswith(("{", "[")):
                try:
                    stack.append((json.loads(preview), depth + 1))
                except Exception:
                    pass

            for nested_value in current.values():
                if isinstance(nested_value, (Mapping, list, tuple)):
                    stack.append((nested_value, depth + 1))
        elif isinstance(current, (list, tuple)):
            for nested_value in reversed(current):
                if isinstance(nested_value, (Mapping, list, tuple)):
                    stack.append((nested_value, depth + 1))


def _append_unique_presenter_line(
    lines: list[str],
    seen: set[str],
    line: str | None,
    *,
    limit: int,
) -> None:
    cleaned = line.strip() if isinstance(line, str) else ""
    if not cleaned or cleaned in seen or len(lines) >= limit:
        return
    seen.add(cleaned)
    lines.append(cleaned)


def _extract_nested_workflow_tool_evidence(
    tool_name: str,
    payload: dict[str, Any],
    *,
    max_lines: int = 8,
) -> dict[str, Any]:
    """Project represented nested workflow progress facts for presenter fallback."""

    projected = project_nested_workflow_progress_evidence(
        payload,
        max_facts=max_lines,
    )
    if not isinstance(projected, Mapping):
        return {
            "workflow_evidence_seen": bool(
                (tool_name or "").lower().startswith("workflow_")
            ),
            "progress_lines": [],
            "contract_ids": [],
            "telemetry": None,
        }

    progress_lines: list[str] = []
    progress_seen: set[str] = set()
    for fact in projected.get("facts") or []:
        if not isinstance(fact, Mapping):
            continue
        line = _format_represented_progress_fact_line(fact)
        _append_unique_presenter_line(
            progress_lines,
            progress_seen,
            line,
            limit=max_lines,
        )

    return {
        "workflow_evidence_seen": bool(projected.get("workflow_evidence_seen")),
        "progress_lines": progress_lines,
        "contract_ids": list(projected.get("contract_ids") or []),
        "telemetry": projected.get("telemetry"),
    }


def _extract_surfaceable_artefact_tool_evidence(
    payload: Mapping[str, Any],
    *,
    max_lines: int = 12,
) -> dict[str, Any]:
    """Project durable artefact handles from structured tool payloads."""

    if not isinstance(payload, Mapping):
        return {"evidence": [], "lines": [], "telemetry": None}

    candidates: list[dict[str, Any]] = []

    def _looks_like_raw_workflow_aggregate(value: Mapping[str, Any]) -> bool:
        workflow_markers = {
            "iteration_results",
            "subworkflow_invocation",
            "subworkflow_result_envelope",
            "workflow_terminal",
            "workflow_terminal_state",
            "tool_invocations",
            "last_action_outputs",
        }
        stack: list[tuple[Any, int]] = [(value, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > 5:
                continue
            if isinstance(current, Mapping):
                if any(marker in current for marker in workflow_markers):
                    return True
                for nested in current.values():
                    if isinstance(nested, (Mapping, list, tuple)):
                        stack.append((nested, depth + 1))
            elif isinstance(current, (list, tuple)):
                for nested in current:
                    if isinstance(nested, (Mapping, list, tuple)):
                        stack.append((nested, depth + 1))
        return False

    raw_surfaceable = payload.get("_surfaceable_concepts")
    raw_surfaceable_present = isinstance(raw_surfaceable, Sequence) and not isinstance(
        raw_surfaceable, (str, bytes, bytearray)
    )
    if raw_surfaceable_present:
        for entry in raw_surfaceable:
            if isinstance(entry, Mapping):
                candidates.append(dict(entry))

    if raw_surfaceable_present or not _looks_like_raw_workflow_aggregate(payload):
        try:
            candidates.extend(
                project_surfaceable_concept_evidence(
                    payload,
                    max_items=max(max_lines * 2, max_lines),
                )
            )
        except Exception:
            pass

    by_concept_id: dict[str, dict[str, Any]] = {}
    for entry in candidates:
        concept_id = _progress_str(entry.get("concept_id"))
        if not concept_id or not concept_id.startswith("#V#"):
            continue
        key = concept_id.lower()
        existing = by_concept_id.get(key)
        if existing is None:
            by_concept_id[key] = dict(entry)
            by_concept_id[key]["concept_id"] = concept_id
            continue
        for field, value in entry.items():
            if field not in existing and value is not None:
                existing[field] = value

    evidence = list(by_concept_id.values())[:max_lines]
    lines = render_surfaceable_concept_lines(
        evidence,
        max_lines=max_lines,
        include_source_paths=True,
        include_verification_status=True,
    )
    if not evidence and not lines:
        return {"evidence": [], "lines": [], "telemetry": None}

    source_paths = [
        source_path
        for entry in evidence
        if (source_path := _progress_str(entry.get("source_path")))
    ]
    verification_keys = [
        verification_key
        for entry in evidence
        if (verification_key := _progress_str(entry.get("verification_key")))
    ]
    telemetry = {
        "schema_version": "surfaceable_artefact_handle_projection.v1",
        "handle_count": len(evidence),
        "line_count": len(lines),
        "concept_ids": [
            concept_id
            for entry in evidence
            if (concept_id := _progress_str(entry.get("concept_id")))
        ],
        "source_paths": source_paths,
        "verification_keys": verification_keys,
        "verified_count": sum(1 for entry in evidence if bool(entry.get("verified"))),
    }
    return {"evidence": evidence, "lines": lines, "telemetry": telemetry}


def _format_represented_progress_fact_line(fact: Mapping[str, Any]) -> str | None:
    label = _progress_str(fact.get("label"))
    if not label:
        return None
    redacted = bool(fact.get("redacted"))
    status = _progress_str(fact.get("status")) or "missing"
    value = fact.get("value")
    if redacted:
        value_text = "[redacted]"
    elif isinstance(value, (str, int, float, bool)) and not isinstance(value, bool):
        value_text = str(value)
    elif isinstance(value, bool):
        value_text = "true" if value else "false"
    elif isinstance(value, list):
        value_text = ", ".join(str(item) for item in value if item is not None)
    else:
        value_text = None

    contract_id = _progress_str(fact.get("contract_id"))
    provenance_bits = [
        bit
        for bit in (
            _progress_str(fact.get("workflow_id")),
            _progress_str(fact.get("state_id")),
            _progress_str(fact.get("action_id")),
        )
        if bit
    ]
    suffix_parts: list[str] = []
    if contract_id:
        suffix_parts.append(f"contract `{contract_id}`")
    if provenance_bits:
        suffix_parts.append(
            "source " + " / ".join(f"`{bit}`" for bit in provenance_bits)
        )
    suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
    if value_text:
        return f"- {label}: `{value_text}`{suffix}."
    reason_code = _progress_str(fact.get("reason_code"))
    if reason_code:
        return f"- {label}: `{status}` ({reason_code}){suffix}."
    return f"- {label}: `{status}`{suffix}."


def _build_presenter_screen_summary_from_tool_messages(
    tool_messages: list[dict],
    *,
    projection_telemetry: dict[str, Any] | None = None,
) -> str | None:
    """Deterministic, user-facing summary of tool activity.

    This deliberately avoids embedding raw JSON payloads so it can safely appear
    in the main chat transcript.
    """

    if not tool_messages:
        return None

    import json

    lines: list[str] = []
    lines.append("Tool activity diagnostics:")

    description_write_seen = False
    relationship_write_seen = False
    names_write_seen = False
    concept_create_seen = False
    nested_workflow_evidence_seen = False
    nested_workflow_lines: list[str] = []
    nested_line_seen: set[str] = set()
    surfaceable_artefact_lines: list[str] = []
    surfaceable_artefact_line_seen: set[str] = set()
    represented_contract_ids: list[str] = []
    represented_contract_seen: set[str] = set()
    represented_projection_events: list[dict[str, Any]] = []
    surfaceable_projection_events: list[dict[str, Any]] = []

    index = 0
    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        parsed = None
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None

        if not isinstance(parsed, dict):
            index += 1
            lines.append(f"{index}. Tool result (unstructured)")
            continue

        tool_name = parsed.get("tool") or parsed.get("method") or "tool"
        status = parsed.get("status")
        error = parsed.get("error")
        payload = parsed.get("payload")

        tool_name_text = tool_name.strip() if isinstance(tool_name, str) else ""
        tool_name_lower = tool_name_text.lower()
        payload_dict = payload if isinstance(payload, dict) else {}

        if "description" in tool_name_lower:
            description_write_seen = True
        predicate = payload_dict.get("predicate")
        if isinstance(predicate, str) and predicate.strip() in {
            "hasDescription",
            "has_description",
            "#V#hasDescription",
        }:
            description_write_seen = True
        description_value = payload_dict.get("description")
        if isinstance(description_value, str) and description_value.strip():
            description_write_seen = True

        if tool_name_lower in {"add_relationship", "remove_relationship"}:
            relationship_write_seen = True
        if tool_name_lower in {"add_names", "add_name"}:
            names_write_seen = True
        if tool_name_lower in {"create_concepts", "create_concept"}:
            concept_create_seen = True

        index += 1
        entry = f"{index}. {tool_name}"
        if status:
            entry += f" — {status}"
        lines.append(entry)
        if error:
            lines.append(f"   Error: {error}")

        # Pull out a few common, safe identifiers to help the user.
        if isinstance(payload, dict):
            for key in ("concept_id", "identifier", "id", "url", "arxiv_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    lines.append(f"   {key}: {value.strip()}")
            if tool_name_lower in {"create_concepts", "create_concept"}:
                concept_labels = _extract_created_concept_labels_from_payload(payload)
                if concept_labels:
                    lines.append(f"   created: {', '.join(concept_labels)}")

            surfaceable_evidence = _extract_surfaceable_artefact_tool_evidence(
                payload,
                max_lines=12,
            )
            surfaceable_telemetry = surfaceable_evidence.get("telemetry")
            if isinstance(surfaceable_telemetry, Mapping):
                surfaceable_projection_events.append(dict(surfaceable_telemetry))
            for surfaceable_line in surfaceable_evidence.get("lines", []):
                _append_unique_presenter_line(
                    surfaceable_artefact_lines,
                    surfaceable_artefact_line_seen,
                    surfaceable_line,
                    limit=12,
                )

            nested_evidence = _extract_nested_workflow_tool_evidence(
                tool_name_text, payload
            )
            nested_workflow_evidence_seen = nested_workflow_evidence_seen or bool(
                nested_evidence.get("workflow_evidence_seen")
            )
            for contract_id in nested_evidence.get("contract_ids") or []:
                if (
                    isinstance(contract_id, str)
                    and contract_id not in represented_contract_seen
                ):
                    represented_contract_seen.add(contract_id)
                    represented_contract_ids.append(contract_id)
            telemetry = nested_evidence.get("telemetry")
            if isinstance(telemetry, Mapping):
                represented_projection_events.append(dict(telemetry))
            for nested_line in nested_evidence.get("progress_lines", []):
                _append_unique_presenter_line(
                    nested_workflow_lines,
                    nested_line_seen,
                    nested_line,
                    limit=12,
                )

    if index == 0:
        return None

    if surfaceable_artefact_lines:
        lines.append("")
        lines.append("Surfaceable artefact handles:")
        lines.extend(surfaceable_artefact_lines)

    write_activity_seen = any(
        (
            description_write_seen,
            relationship_write_seen,
            names_write_seen,
            concept_create_seen,
        )
    )
    lines.append("")
    if write_activity_seen:
        lines.append("Write activity (authoritative):")
        if description_write_seen:
            lines.append(
                "- Description updated: YES (evidence present in tool results)"
            )
        if relationship_write_seen:
            lines.append("- Relationship writes detected")
        if names_write_seen:
            lines.append("- Name writes detected")
        if concept_create_seen:
            lines.append("- Concept creation detected")
    elif surfaceable_artefact_lines:
        lines.append("Write activity notes:")
        lines.append(
            "- No top-level write-tool ledger entries were detected; use the artefact handles above as the primary durable-result evidence."
        )
    elif nested_workflow_evidence_seen and nested_workflow_lines:
        lines.append("Top-level write-tool summary was unavailable.")
        lines.append(
            "- Use the represented workflow progress facts below for any durable-effect claims."
        )
    elif nested_workflow_evidence_seen:
        lines.append("Nested workflow payloads included no represented progress facts.")
        lines.append(
            "- No direct top-level write-tool summary was available; do not infer completion or durable effects from this fallback."
        )
    else:
        lines.append("No write activity was detected in the tool results.")

    if nested_workflow_lines:
        lines.append("")
        lines.append("Represented workflow progress facts:")
        lines.extend(nested_workflow_lines)

    if isinstance(projection_telemetry, dict):
        projection_telemetry.update(
            {
                "schema_version": "presenter_nested_workflow_progress_projection.v1",
                "contract_ids": represented_contract_ids,
                "projection_events": represented_projection_events,
                "fact_count": len(nested_workflow_lines),
                "workflow_evidence_seen": bool(nested_workflow_evidence_seen),
                "surfaceable_artefact_handle_count": len(surfaceable_artefact_lines),
                "surfaceable_projection_events": surfaceable_projection_events,
            }
        )

    return "\n".join(lines).strip() or None


def _latest_workflow_execution_summary_from_aux_calls(
    aux_calls: Any,
) -> dict[str, Any] | None:
    if not isinstance(aux_calls, (list, tuple)):
        return None

    for entry in reversed(aux_calls):
        if not isinstance(entry, Mapping):
            continue
        call_type = entry.get("type")
        if not isinstance(call_type, str) or call_type.strip() != "workflow_execution":
            continue

        summary_raw = entry.get("execution_summary")
        summary = dict(summary_raw) if isinstance(summary_raw, Mapping) else {}
        if not summary:
            for key in (
                "workflow_id",
                "workflow_instance_id",
                "completed",
                "effective_completed",
                "terminal_status",
                "final_state",
                "action_started_count",
                "action_completed_count",
                "action_success_count",
                "action_failure_count",
                "action_unknown_count",
                "terminal_effect_count",
                "durable_side_effect_count",
            ):
                if key in entry:
                    summary[key] = entry.get(key)
        workflow_id = _progress_str(summary.get("workflow_id")) or _progress_str(
            entry.get("workflow_id")
        )
        if workflow_id:
            summary["workflow_id"] = workflow_id
        return summary or None

    return None


def _coerce_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        return 0
    return max(0, parsed)


def _workflow_execution_summary_has_progress(
    execution_summary: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(execution_summary, Mapping):
        return False
    if any(
        _coerce_non_negative_int(execution_summary.get(key)) > 0
        for key in (
            "action_started_count",
            "action_completed_count",
            "action_success_count",
            "action_failure_count",
            "terminal_effect_count",
            "durable_side_effect_count",
            "runtime_event_count",
            "step_result_envelope_count",
        )
    ):
        return True
    return any(
        bool(_progress_str(execution_summary.get(key)))
        for key in (
            "workflow_id",
            "workflow_instance_id",
            "terminal_status",
            "final_state",
            "workflow_instance_status",
        )
    )


def _build_presenter_workflow_execution_follow_up_summary(
    workflow_execution_summary: Mapping[str, Any] | None,
    *,
    completion_gate: Mapping[str, Any] | None = None,
) -> str | None:
    if not _workflow_execution_summary_has_progress(workflow_execution_summary):
        return None

    summary = workflow_execution_summary or {}
    completion_gate_map = (
        completion_gate if isinstance(completion_gate, Mapping) else {}
    )
    requires_follow_up = bool(completion_gate_map.get("requires_follow_up", False))
    safe_to_claim_completion = bool(
        completion_gate_map.get("safe_to_claim_completion", not requires_follow_up)
    )

    workflow_id = _progress_str(summary.get("workflow_id"))
    workflow_instance_id = _progress_str(summary.get("workflow_instance_id"))
    terminal_status = _progress_str(summary.get("terminal_status"))
    final_state = _progress_str(summary.get("final_state"))
    completed = summary.get("completed")
    effective_completed = summary.get("effective_completed")
    action_success_count = _coerce_non_negative_int(summary.get("action_success_count"))
    action_failure_count = _coerce_non_negative_int(summary.get("action_failure_count"))
    action_unknown_count = _coerce_non_negative_int(summary.get("action_unknown_count"))
    action_completed_count = _coerce_non_negative_int(
        summary.get("action_completed_count")
    )
    terminal_effect_count = _coerce_non_negative_int(
        summary.get("terminal_effect_count")
    )
    durable_side_effect_count = _coerce_non_negative_int(
        summary.get("durable_side_effect_count")
    )

    if terminal_status:
        outcome = f"The selected workflow reached terminal status `{terminal_status}`."
    elif isinstance(completed, bool):
        outcome = (
            "The selected workflow reported completion."
            if completed
            else "The selected workflow reported that it did not complete."
        )
    else:
        outcome = "The selected workflow produced execution evidence."

    lines = ["Workflow-backed outcome:", outcome]
    detail_bits: list[str] = []
    if workflow_id:
        detail_bits.append(f"workflow `{workflow_id}`")
    if workflow_instance_id:
        detail_bits.append(f"instance `{workflow_instance_id}`")
    if final_state:
        detail_bits.append(f"final state `{final_state}`")
    if isinstance(effective_completed, bool):
        detail_bits.append(f"effective completed: {str(effective_completed).lower()}")
    if detail_bits:
        lines.append(f"- {'; '.join(detail_bits)}")

    action_bits: list[str] = []
    if action_completed_count > 0:
        action_bits.append(f"{action_completed_count} completed")
    if action_success_count > 0:
        action_bits.append(f"{action_success_count} succeeded")
    if action_failure_count > 0:
        action_bits.append(f"{action_failure_count} failed")
    if action_unknown_count > 0:
        action_bits.append(f"{action_unknown_count} unknown")
    if action_bits:
        lines.append(f"- Actions: {', '.join(action_bits)}")
    lines.append(f"- Terminal effects observed: {terminal_effect_count}")
    lines.append(f"- Durable side effects observed: {durable_side_effect_count}")

    if requires_follow_up or not safe_to_claim_completion:
        lines.append("")
        lines.append(
            "Verification still needs follow-up before Von should claim the user task is complete."
        )
        blocking_failure_codes: list[str] = []
        for item in completion_gate_map.get("blocking_failure_codes", []):
            blocking_failure_code = _progress_str(item)
            if blocking_failure_code:
                blocking_failure_codes.append(blocking_failure_code)
        if blocking_failure_codes:
            lines.append(
                "- Blocking failure codes: " + ", ".join(blocking_failure_codes)
            )
        unresolved_preconditions = completion_gate_map.get("unresolved_preconditions")
        if isinstance(unresolved_preconditions, list):
            unresolved_bits: list[str] = []
            for item in unresolved_preconditions:
                if not isinstance(item, Mapping):
                    continue
                effect_type = _progress_str(item.get("effect_type"))
                status = _progress_str(item.get("status"))
                status_reason = _progress_str(item.get("status_reason"))
                bit = " ".join(value for value in (effect_type, status) if value)
                if status_reason:
                    bit = f"{bit}: {status_reason}" if bit else status_reason
                if bit:
                    unresolved_bits.append(bit)
            if unresolved_bits:
                lines.append("- Unresolved evidence: " + "; ".join(unresolved_bits[:4]))

    return "\n".join(lines).strip()


def _build_presenter_follow_up_summary_from_tool_messages(
    tool_messages: list[dict],
    *,
    completion_gate: Mapping[str, Any] | None = None,
    auxiliary_llm_calls: list[dict[str, Any]] | None = None,
    workflow_execution_summary: Mapping[str, Any] | None = None,
) -> str | None:
    """Build one shared presenter summary basis for incomplete tool turns."""

    tool_summary = _build_presenter_screen_summary_from_tool_messages(tool_messages)
    if workflow_execution_summary is None:
        workflow_execution_summary = _latest_workflow_execution_summary_from_aux_calls(
            auxiliary_llm_calls
        )
    workflow_summary = _build_presenter_workflow_execution_follow_up_summary(
        workflow_execution_summary,
        completion_gate=completion_gate,
    )
    if not isinstance(completion_gate, Mapping):
        return (
            "\n\n".join(item for item in (workflow_summary, tool_summary) if item)
            or None
        )

    requires_follow_up = bool(completion_gate.get("requires_follow_up", False))
    safe_to_claim_completion = bool(
        completion_gate.get("safe_to_claim_completion", not requires_follow_up)
    )
    if not requires_follow_up and safe_to_claim_completion:
        return (
            "\n\n".join(item for item in (workflow_summary, tool_summary) if item)
            or None
        )

    if workflow_summary:
        lines = [workflow_summary]
    else:
        lines = [
            "I ran tools for this request, but I do not have a reliable final answer yet.",
            "This turn still needs follow-up before it should be treated as complete.",
        ]

    decision_reason = _progress_str(completion_gate.get("decision_reason"))
    decision_reason_is_internal_status = _looks_like_internal_status_diagnostic(
        decision_reason
    )
    decision_reason_is_overbroad_mutation_status = bool(
        workflow_summary
        and decision_reason
        and decision_reason.strip().lower()
        in {
            "required mutation was not executed.",
            "mutation attempt failed or was blocked.",
        }
    )
    if decision_reason and decision_reason_is_internal_status:
        _append_presenter_detector_event(
            auxiliary_llm_calls,
            function_name="_build_presenter_follow_up_summary_from_tool_messages",
            detector="internal_status_diagnostic",
            context="follow_up_decision_reason",
            reason_code="follow_up_decision_reason_suppressed",
            preview=decision_reason,
        )
    if decision_reason and decision_reason_is_overbroad_mutation_status:
        _append_presenter_detector_event(
            auxiliary_llm_calls,
            function_name="_build_presenter_follow_up_summary_from_tool_messages",
            detector="workflow_execution_reconciled",
            context="follow_up_decision_reason",
            reason_code="overbroad_mutation_status_replaced_by_workflow_evidence",
            preview=decision_reason,
        )
    if (
        decision_reason
        and not decision_reason_is_internal_status
        and not decision_reason_is_overbroad_mutation_status
    ):
        lines.append(decision_reason)

    if tool_summary:
        lines.append("")
        lines.append(tool_summary)

    return "\n\n".join(line.strip() for line in lines if line and line.strip())


def _build_tool_messages_prompt_blob(
    tool_messages: list[dict], *, max_chars: int = 12000
) -> str:
    """Build a compact plain-text representation of tool results for LLM backfill."""

    if not tool_messages:
        return "(no tool messages)"

    import json

    def _normalise_tool_name(parsed: dict) -> str:
        tool_name = (
            parsed.get("tool") or parsed.get("method") or parsed.get("name") or ""
        )
        return tool_name.strip() if isinstance(tool_name, str) else ""

    def _iter_parsed_tool_results(messages: list[dict]) -> list[dict]:
        parsed_results: list[dict] = []
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            try:
                parsed = json.loads(content)
            except Exception:
                continue
            if isinstance(parsed, dict):
                parsed_results.append(parsed)
        return parsed_results

    parsed_results = _iter_parsed_tool_results(tool_messages)

    executed_lines: list[str] = []
    writes_lines: list[str] = []
    surfaceable_artefact_lines: list[str] = []
    relation_evidence_lines: list[str] = []
    nested_workflow_lines: list[str] = []
    surfaceable_artefact_seen: set[str] = set()
    nested_workflow_evidence_seen = False
    nested_line_seen: set[str] = set()
    nested_contract_ids: list[str] = []
    nested_contract_seen: set[str] = set()

    # Track a small set of write categories we care about for UI truthfulness.
    description_write_seen = False
    relationship_write_seen = False
    names_write_seen = False
    concept_create_seen = False

    def _mark_description_write(tool_name: str, payload: dict) -> None:
        nonlocal description_write_seen
        if description_write_seen:
            return
        tool_name_lower = (tool_name or "").lower()
        if "description" in tool_name_lower:
            description_write_seen = True
            return
        predicate = payload.get("predicate")
        if isinstance(predicate, str) and predicate.strip() in {
            "hasDescription",
            "has_description",
            "#V#hasDescription",
        }:
            description_write_seen = True
            return
        description_value = payload.get("description")
        if isinstance(description_value, str) and description_value.strip():
            description_write_seen = True

    def _summarise_relationship_write(tool_name: str, payload: dict) -> str | None:
        nonlocal relationship_write_seen
        name_lower = (tool_name or "").lower()
        if name_lower not in {"add_relationship", "remove_relationship"}:
            return None

        source_id = payload.get("source_id")
        predicate = payload.get("predicate")
        target = payload.get("target")
        added = payload.get("added")
        removed = payload.get("removed")

        if not (
            isinstance(source_id, str)
            and isinstance(predicate, str)
            and isinstance(target, str)
        ):
            relationship_write_seen = True
            return f"- Relationship update via {tool_name} (details unavailable)"

        relationship_write_seen = True

        verb = "changed"
        if name_lower == "add_relationship":
            verb = "added" if added is not False else "attempted"
        elif name_lower == "remove_relationship":
            verb = "removed" if removed is not False else "attempted"

        return f"- Relationship {verb}: `{source_id}` — `{predicate}` → `{target}`"

    def _summarise_name_or_concept_write(tool_name: str, payload: dict) -> str | None:
        nonlocal names_write_seen, concept_create_seen
        name_lower = (tool_name or "").lower()
        if name_lower in {"add_names", "add_name"}:
            names_write_seen = True
            concept_id = payload.get("concept_id")
            if isinstance(concept_id, str) and concept_id.strip():
                return f"- Names added for `{concept_id}`"
            return "- Names added"

        if name_lower in {"create_concepts", "create_concept"}:
            concept_create_seen = True
            concept_labels = _extract_created_concept_labels_from_payload(payload)
            if concept_labels:
                suffix = ""
                total_created = payload.get("successful")
                if isinstance(total_created, int) and total_created > len(
                    concept_labels
                ):
                    suffix = f" (+{total_created - len(concept_labels)} more)"
                return f"- Concepts created: {', '.join(concept_labels)}{suffix}"
            total = payload.get("total")
            if isinstance(total, int):
                return f"- Concepts created: {total}"
            return "- Concept creation attempted"

        return None

    def _first_non_empty_text(*values: object) -> str | None:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    import re

    concept_token_pattern = re.compile(r"#V#[A-Za-z0-9_]+")

    def _extract_concept_id_from_text(text: str) -> str | None:
        if not isinstance(text, str):
            return None
        candidate = text.strip()
        if not candidate:
            return None
        if candidate.startswith("#V#"):
            return candidate
        match = concept_token_pattern.search(candidate)
        if not match:
            return None
        token = match.group(0)
        return token.rstrip("`.,;:)]}>")

    def _extract_target_concept_id(hit: Mapping[str, Any]) -> str | None:
        candidate_keys = (
            "target_concept_id",
            "target_id",
            "target",
            "object",
            "object_id",
            "target_value",
            "target_value_preview",
        )
        for key in candidate_keys:
            candidate = _first_non_empty_text(hit.get(key))
            extracted = _extract_concept_id_from_text(candidate or "")
            if extracted:
                return extracted

        target_preview = hit.get("target_concept_preview")
        if isinstance(target_preview, Mapping):
            for preview_key in (
                "concept_id",
                "id",
                "target_id",
                "target_concept_id",
            ):
                candidate = _first_non_empty_text(target_preview.get(preview_key))
                extracted = _extract_concept_id_from_text(candidate or "")
                if extracted:
                    return extracted

        # Broad fallback for odd payloads that only embed concept IDs inline.
        fallback_keys = {
            "target",
            "target_id",
            "target_name",
            "target_value",
            "target_value_preview",
            "target_concept_preview",
        }
        for key in fallback_keys:
            if key not in hit:
                continue
            value = hit.get(key)
            if isinstance(value, str):
                extracted = _extract_concept_id_from_text(value)
                if extracted:
                    return extracted
            elif isinstance(value, Mapping):
                for nested_value in value.values():
                    extracted = _extract_concept_id_from_text(
                        nested_value if isinstance(nested_value, str) else ""
                    )
                    if extracted:
                        return extracted
        return None

    def _append_relation_evidence(tool_name: str, payload: dict) -> None:
        if (tool_name or "").lower() != "find_relations_with_argument":
            return
        raw_hits = payload.get("hits")
        if not isinstance(raw_hits, list):
            return

        for hit in raw_hits[:20]:
            if not isinstance(hit, dict):
                continue
            source_id = _first_non_empty_text(hit.get("source_concept_id"))
            predicate_id = _first_non_empty_text(hit.get("predicate_concept_id"))
            target_id = _extract_target_concept_id(hit)

            if not (source_id and predicate_id and target_id):
                continue

            labels: list[str] = []
            source_name = _first_non_empty_text(hit.get("source_name"))
            if source_name and source_name != source_id:
                labels.append(f"source_label={source_name}")
            target_name = _first_non_empty_text(hit.get("target_name"))
            if target_name and target_name != target_id:
                labels.append(f"target_label={target_name}")
            label_suffix = f" ({'; '.join(labels)})" if labels else ""
            relation_evidence_lines.append(
                f"- source_concept_id={source_id}; "
                f"predicate_concept_id={predicate_id}; "
                f"target_concept_id={target_id}{label_suffix}"
            )

    for parsed in parsed_results:
        tool_name = _normalise_tool_name(parsed) or "tool"
        status = parsed.get("status")
        status_text = (
            status.strip() if isinstance(status, str) and status.strip() else None
        )
        executed_lines.append(
            f"- {tool_name}" + (f" ({status_text})" if status_text else "")
        )

        payload = parsed.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        # Categorise writes.
        rel_summary = _summarise_relationship_write(tool_name, payload)
        if rel_summary:
            writes_lines.append(rel_summary)

        name_or_concept_summary = _summarise_name_or_concept_write(tool_name, payload)
        if name_or_concept_summary:
            writes_lines.append(name_or_concept_summary)

        _append_relation_evidence(tool_name, payload)
        _mark_description_write(tool_name, payload)
        surfaceable_evidence = _extract_surfaceable_artefact_tool_evidence(
            payload,
            max_lines=16,
        )
        for surfaceable_line in surfaceable_evidence.get("lines", []):
            _append_unique_presenter_line(
                surfaceable_artefact_lines,
                surfaceable_artefact_seen,
                surfaceable_line,
                limit=16,
            )
        nested_evidence = _extract_nested_workflow_tool_evidence(tool_name, payload)
        nested_workflow_evidence_seen = nested_workflow_evidence_seen or bool(
            nested_evidence.get("workflow_evidence_seen")
        )
        for contract_id in nested_evidence.get("contract_ids") or []:
            if isinstance(contract_id, str) and contract_id not in nested_contract_seen:
                nested_contract_seen.add(contract_id)
                nested_contract_ids.append(contract_id)
        for nested_line in nested_evidence.get("progress_lines", []):
            _append_unique_presenter_line(
                nested_workflow_lines,
                nested_line_seen,
                nested_line,
                limit=16,
            )

    if description_write_seen:
        writes_lines.append(
            "- Description updated: YES (evidence present in tool results)"
        )

    did_not_lines: list[str] = []
    if not relationship_write_seen:
        did_not_lines.append(
            "- No top-level relationship write evidence detected"
            if nested_workflow_evidence_seen
            else "- No relationship writes detected"
        )
    if not names_write_seen:
        did_not_lines.append(
            "- No top-level name write evidence detected"
            if nested_workflow_evidence_seen
            else "- No name writes detected"
        )
    if not concept_create_seen:
        did_not_lines.append(
            "- No top-level concept creation evidence detected; surfaceable artefact handles above may still identify verified or existing represented concepts."
            if surfaceable_artefact_lines
            else (
                "- No top-level concept creation evidence detected"
                if nested_workflow_evidence_seen
                else "- No concept creation detected"
            )
        )

    blob_lines: list[str] = []
    blob_lines.append("TOOL EXECUTION (authoritative):")
    blob_lines.extend(executed_lines or ["- (no parsed tool results)"])
    if surfaceable_artefact_lines:
        blob_lines.append("")
        blob_lines.append("SURFACEABLE ARTEFACT HANDLES (authoritative evidence):")
        blob_lines.extend(surfaceable_artefact_lines)
    blob_lines.append("")
    blob_lines.append("TOOL WRITES LEDGER (authoritative):")
    blob_lines.extend(
        writes_lines
        or (
            [
                "- No top-level write-ledger entries detected; see surfaceable artefact handles above for durable-result evidence."
            ]
            if surfaceable_artefact_lines
            else ["- No writes detected"]
        )
    )
    if nested_workflow_evidence_seen:
        blob_lines.append("")
        blob_lines.append("REPRESENTED WORKFLOW PROGRESS FACTS:")
        if nested_contract_ids:
            blob_lines.append(
                "- projection_contract_ids="
                + ", ".join(f"`{contract_id}`" for contract_id in nested_contract_ids)
            )
        blob_lines.extend(
            nested_workflow_lines
            or [
                "- Nested workflow/subworkflow payloads were present, but no represented progress facts were supplied."
            ]
        )
    if relation_evidence_lines:
        blob_lines.append("")
        blob_lines.append("TOOL RELATION EVIDENCE (authoritative):")
        blob_lines.extend(relation_evidence_lines)
    if did_not_lines:
        blob_lines.append("")
        blob_lines.append("WRITES NOT DETECTED:")
        blob_lines.extend(did_not_lines)

    blob = "\n".join(blob_lines).strip() or "(tool results unavailable)"

    blob = str(blob)
    if len(blob) > max_chars:
        blob = blob[:max_chars].rstrip() + "\n... [truncated]"
    return blob


def _is_prompt_introspection_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "what is my user prompt",
        "what's my user prompt",
        "what is my system prompt",
        "what's my system prompt",
        "what prompt is active",
        "which prompt is active",
        "tell me what my user prompt is",
        "tell me what my system prompt is",
    )
    return any(t in lowered for t in triggers)


def _is_tool_introspection_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "what tools do you have",
        "what tools can you",
        "what tools can i",
        "list tools",
        "show tools",
        "available tools",
        "tool list",
        "mcp tools",
        "what can you do",
        "what capabilities do you have",
    )
    return any(t in lowered for t in triggers)


def _is_rag_status_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "rag status",
        "what is my rag status",
        "is rag enabled",
        "is rag on",
        "rag enabled",
        "rag on",
        "rag working",
        "rag isolation",
    )
    return any(t in lowered for t in triggers)


def _format_tool_inventory(methods: dict) -> str:
    # Deterministic plain-text listing; keep stable ordering.
    if not isinstance(methods, dict) or not methods:
        return "No internal MCP tools are registered."

    # Group by category
    buckets: dict[str, list[tuple[str, str]]] = {}
    for name, meta in methods.items():
        if not isinstance(name, str):
            continue
        meta_dict = meta if isinstance(meta, dict) else {}

        category: str = "other"
        category_value = meta_dict.get("category")
        if isinstance(category_value, str) and category_value.strip():
            category = category_value.strip()

        desc: str = ""
        desc_value = meta_dict.get("description")
        if isinstance(desc_value, str):
            desc = desc_value.strip()

        buckets.setdefault(category, []).append((name, desc))

    lines: list[str] = []
    lines.append("Internal MCP tools currently registered:")
    for category in sorted(buckets.keys()):
        lines.append("")
        lines.append(f"- {category}:")
        for name, desc in sorted(buckets[category], key=lambda x: x[0]):
            if desc:
                lines.append(f"  - {name}: {desc}")
            else:
                lines.append(f"  - {name}")
    lines.append("")
    lines.append(
        "Note: Some tools (especially RAG) require a user namespace to avoid cross-user data leakage."
    )
    return "\n".join(lines)


def _deterministic_introspection_enabled() -> bool:
    try:
        flag_value = os.getenv("VON_DETERMINISTIC_INTROSPECTION", "0")
        return str(flag_value).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _build_chat_history_context_kwargs(
    *,
    namespace: str | None,
    organisation_concept_id: str | None,
    role_in_org: str | None,
) -> dict[str, Any]:
    context_kwargs: dict[str, Any] = {}
    if isinstance(namespace, str) and namespace.strip():
        context_kwargs["namespace"] = namespace.strip()
    if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
        context_kwargs["organisation_concept_id"] = organisation_concept_id.strip()
    if isinstance(role_in_org, str) and role_in_org.strip():
        context_kwargs["role_in_org"] = role_in_org.strip()
    return context_kwargs


def _add_chat_history_message(
    *,
    user_id: str,
    session_id: str,
    message: dict[str, Any],
    llm_debug_data: dict[str, Any] | None = None,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
    role_in_org: str | None = None,
    skip_rag_indexing: bool = False,
) -> None:
    context_kwargs = _build_chat_history_context_kwargs(
        namespace=namespace,
        organisation_concept_id=organisation_concept_id,
        role_in_org=role_in_org,
    )
    chat_history_service.add_message_to_history(
        user_id,
        session_id,
        message,
        llm_debug_data=llm_debug_data,
        skip_rag_indexing=skip_rag_indexing,
        **context_kwargs,
    )


def _namespace_is_org_scoped(namespace: str | None) -> bool:
    return (
        isinstance(namespace, str)
        and namespace.strip().startswith("#V#")
        and ("@" in namespace.strip())
    )


def _record_namespace_isolation_observation(
    *,
    flow: str,
    namespace: str | None,
    namespace_source: str | None,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    mismatch_detected: bool,
    details: dict[str, Any] | None = None,
) -> None:
    """Best-effort namespace isolation telemetry for runtime diagnostics."""
    try:
        from ...services.namespace_isolation_diagnostics_service import (
            record_namespace_context_observation,
        )

        record_namespace_context_observation(
            flow=flow,
            namespace=namespace,
            namespace_source=namespace_source,
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            mismatch_detected=bool(mismatch_detected),
            details=details,
        )
    except Exception:
        # Diagnostics must never break request handling.
        pass


def _resolve_generate_namespace_context(
    *,
    user_concept_id: str | None,
    effective_context: dict[str, Any] | None,
    flask_session_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Resolve generate-time namespace with provenance and mismatch diagnostics.

    Ordering strategy:
    1. Preserve effective window/flask context namespace.
    2. Prefer an org-scoped namespace when any org context is present.
    3. Fall back to user-only namespace derivation.
    """

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add_candidate(source: str, namespace_value: Any) -> None:
        if not isinstance(namespace_value, str):
            return
        cleaned = namespace_value.strip()
        if not cleaned:
            return
        if cleaned.startswith("#v#"):
            cleaned = "#V#" + cleaned[3:]
        elif not cleaned.startswith("#V#"):
            cleaned = f"#V#{cleaned.lstrip('#')}"
        if cleaned in seen:
            return
        seen.add(cleaned)
        candidates.append(
            {
                "namespace": cleaned,
                "source": source,
                "org_scoped": _namespace_is_org_scoped(cleaned),
            }
        )

    effective = effective_context if isinstance(effective_context, dict) else {}
    effective_namespace = effective.get("namespace")
    effective_org = effective.get("organisation_id")
    effective_source = effective.get("source")

    _add_candidate("effective_context.namespace", effective_namespace)
    if isinstance(effective_org, str) and effective_org.strip():
        _add_candidate(
            "derived_from_effective_context.org",
            _derive_namespace_for_user_org(user_concept_id, effective_org),
        )

    session_namespace = flask_session_snapshot.get("namespace")
    session_org = flask_session_snapshot.get("organisation_concept_id") or (
        flask_session_snapshot.get("org_id")
    )

    _add_candidate("flask_session.namespace", session_namespace)
    if isinstance(session_org, str) and session_org.strip():
        _add_candidate(
            "derived_from_flask_session.org",
            _derive_namespace_for_user_org(user_concept_id, session_org),
        )
    _add_candidate(
        "derived_from_user_concept_id",
        _derive_namespace_for_user_org(user_concept_id, None),
    )

    org_candidates = [c for c in candidates if c.get("org_scoped")]
    selected = (
        org_candidates[0] if org_candidates else (candidates[0] if candidates else None)
    )

    comparable_candidates = [
        c for c in candidates if c.get("source") != "derived_from_user_concept_id"
    ]
    distinct_namespaces = {
        c.get("namespace")
        for c in (comparable_candidates or candidates)
        if c.get("namespace")
    }
    mismatch_detected = len(distinct_namespaces) > 1

    report = {
        "namespace": selected.get("namespace") if isinstance(selected, dict) else None,
        "namespace_source": (
            selected.get("source") if isinstance(selected, dict) else "missing"
        ),
        "effective_context_source": (
            effective_source if isinstance(effective_source, str) else None
        ),
        "effective_context_namespace": (
            effective_namespace.strip()
            if isinstance(effective_namespace, str) and effective_namespace.strip()
            else None
        ),
        "session_namespace": (
            session_namespace.strip()
            if isinstance(session_namespace, str) and session_namespace.strip()
            else None
        ),
        "candidates": candidates,
        "mismatch_detected": mismatch_detected,
        "org_scope_preferred": bool(org_candidates),
    }
    if mismatch_detected:
        report["mismatch_reason"] = "multiple_namespace_candidates"
    return report


def _set_active_chat_session_for_request(
    *,
    session_id: str,
    user_concept_id: str | None,
    window_session_id: str | None,
) -> None:
    cleaned_session_id = (
        session_id.strip()
        if isinstance(session_id, str) and session_id.strip()
        else None
    )
    if not cleaned_session_id:
        return

    if isinstance(window_session_id, str) and window_session_id.strip():
        from ...services.window_session_context_service import set_window_chat_session

        set_window_chat_session(
            window_session_id.strip(),
            cleaned_session_id,
            user_concept_id,
        )

    session["session_id"] = cleaned_session_id
    session.modified = True


def _normalise_create_chat_session_provenance(
    data: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return safe session provenance kwargs for test/agent-created conversations."""

    body = data if isinstance(data, Mapping) else {}
    provenance: Dict[str, Any] = {}
    for key in (
        "origin_kind",
        "created_by_actor_concept_id",
        "created_by_actor_type",
        "is_agent_created",
        "test_artifact_kind",
    ):
        if key in body:
            provenance[key] = body.get(key)

    is_browser_test_session = session.get(
        "auth_provider"
    ) == "browser_test_fixture" or bool(session.get("browser_test_fixture_id"))
    if is_browser_test_session:
        provenance["origin_kind"] = (
            provenance.get("origin_kind")
            or chat_history_service.CHAT_SESSION_ORIGIN_KIND_BROWSER_TEST_FIXTURE
        )
        provenance["created_by_actor_concept_id"] = (
            provenance.get("created_by_actor_concept_id") or VON_SYSTEM_ID
        )
        provenance["created_by_actor_type"] = (
            provenance.get("created_by_actor_type") or CODING_AGENT_TYPE_ID
        )
        provenance["is_agent_created"] = True
        provenance["test_artifact_kind"] = (
            provenance.get("test_artifact_kind")
            or "browser_test_authenticated_chat_session"
        )

    return provenance


def _ensure_generate_conversation_session(
    *,
    request_conversation_session_id: str | None,
    effective_context: Mapping[str, Any] | None,
    user_concept_id: str | None,
    user_namespace: str | None,
    org_concept_id: str | None,
    role_in_org: str | None,
    window_session_id: str | None,
) -> tuple[str, str | None, bool]:
    explicit_session_candidate = (
        request_conversation_session_id
        if isinstance(request_conversation_session_id, str)
        else None
    )
    explicit_session_id = (
        explicit_session_candidate.strip() if explicit_session_candidate else None
    )
    if explicit_session_id:
        return explicit_session_id, None, False

    effective = effective_context if isinstance(effective_context, Mapping) else {}
    active_window_session_candidate = effective.get("chat_session_id")
    active_window_session_id = (
        active_window_session_candidate.strip()
        if isinstance(active_window_session_candidate, str)
        and active_window_session_candidate.strip()
        else None
    )
    if active_window_session_id:
        return active_window_session_id, None, False

    if isinstance(user_concept_id, str) and user_concept_id.strip():
        created_session_id = str(uuid.uuid4())
        created_session = chat_history_service.create_chat_session(
            user_id=user_concept_id,
            session_id=created_session_id,
            session_name=None,
            namespace=user_namespace,
            organisation_concept_id=org_concept_id,
            role_in_org=role_in_org,
            **_normalise_create_chat_session_provenance(),
        )
        _set_active_chat_session_for_request(
            session_id=created_session_id,
            user_concept_id=user_concept_id,
            window_session_id=window_session_id,
        )
        created_session_name_candidate = (
            created_session.get("session_name")
            if isinstance(created_session, Mapping)
            else None
        )
        created_session_name = (
            created_session_name_candidate.strip()
            if isinstance(created_session_name_candidate, str)
            and created_session_name_candidate.strip()
            else None
        )
        return created_session_id, created_session_name, True

    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    session.modified = True
    return str(session["session_id"]), None, False


def _maybe_handle_prompt_introspection_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str,
    user_namespace: str | None,
    org_concept_id: str | None,
    role_in_org: str | None,
    history_user_id: str | None,
    session_id: str,
    dynamic_instructions: str | None = None,
    auxiliary_system_prompt: str | None = None,
    user_prompt_debug: dict,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
    request_id: str | None = None,
    progress_scope_key: str | None = None,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
    import json as _json

    prompt_namespace = user_namespace or user_concept_id
    if dynamic_instructions is None and isinstance(auxiliary_system_prompt, str):
        dynamic_instructions = auxiliary_system_prompt

    tool_messages: list[dict] = []
    tool_invocations: list[dict] = []

    response_text = None
    used_tool = False

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            tool_result = gateway.invoke(
                "chat_get_prompt_context",
                {
                    "namespace": prompt_namespace,
                    "include_content": True,
                    "max_chars": 5000,
                },
            )
            payload = tool_result.payload
            duration_ms = getattr(tool_result, "duration_ms", None)
            used_tool = True
            tool_messages = [
                {
                    "role": "tool",
                    "content": _json.dumps(
                        {
                            "tool": "chat_get_prompt_context",
                            "status": "ok",
                            "duration_ms": duration_ms,
                            "payload": payload,
                        },
                        default=str,
                    ),
                }
            ]
            tool_invocations = [
                {
                    "tool": "chat_get_prompt_context",
                    "payload": {
                        "namespace": prompt_namespace,
                        "include_content": True,
                        "max_chars": 5000,
                    },
                    "duration_ms": duration_ms,
                    "direct_user_call": False,
                }
            ]

            if isinstance(payload, dict) and payload.get("success"):
                prompt_ids = payload.get("prompt_concept_ids") or []
                prompt_text_value = payload.get("prompt_text") or ""
                if not isinstance(prompt_text_value, str):
                    prompt_text_value = str(prompt_text_value)
                prompt_text_value = prompt_text_value.strip()
                if not prompt_text_value:
                    response_text = (
                        "No user-specific system prompt text is currently available for your account. "
                        "(The prompt linkage exists but no content was returned.)"
                    )
                else:
                    response_text = (
                        "Here is your current user-specific system prompt (from Vontology).\n\n"
                        f"Prompt concept IDs: {prompt_ids}\n\n"
                        f"{prompt_text_value}"
                    )
            else:
                response_text = (
                    "I could not retrieve your user-specific prompt context via internal tools. "
                    f"Result: {payload}"
                )
        except Exception as exc:
            current_app.logger.warning(
                "[mcp_orchestrator] Prompt introspection tool failed: %s", exc
            )

    # Fallback: use already-loaded prompt fragments (no tool required)
    if response_text is None:
        prompt_ids = (
            user_prompt_debug.get("prompt_concept_ids")
            if isinstance(user_prompt_debug, dict)
            else []
        )
        if not isinstance(prompt_ids, list):
            prompt_ids = []
        prompt_text_value = dynamic_instructions or ""
        prompt_text_value = (
            prompt_text_value.strip()
            if isinstance(prompt_text_value, str)
            else str(prompt_text_value)
        )
        if not prompt_text_value:
            response_text = "No user-specific system prompt is currently active (no prompt content was loaded from Vontology)."
        else:
            response_text = (
                "Here is your current user-specific system prompt (from Vontology).\n\n"
                f"Prompt concept IDs: {prompt_ids}\n\n"
                f"{prompt_text_value}"
            )

    # Store messages in history/context, matching the direct-tool-call pattern.
    if history_user_id:
        _add_chat_history_message(
            user_id=history_user_id,
            session_id=session_id,
            message={
                "role": "user",
                "content": prompt_text,
                "author_user_id": user_concept_id,
            },
            namespace=prompt_namespace,
            organisation_concept_id=org_concept_id,
            role_in_org=role_in_org,
        )
    for tool_msg in _truncate_large_tool_results(
        tool_messages, max_tool_content_chars=5000
    ):
        if history_user_id:
            _add_chat_history_message(
                user_id=history_user_id,
                session_id=session_id,
                message=tool_msg,
                namespace=prompt_namespace,
                organisation_concept_id=org_concept_id,
                role_in_org=role_in_org,
            )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    current_turn_messages = [{"role": "user", "content": prompt_text}] + tool_messages
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(current_app.config["CONTEXT"])
    tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None
    tool_progress_snapshot = _snapshot_tool_progress_for_request(
        progress_scope_key, request_id
    )
    turn_execution_diagnostics = _build_turn_execution_diagnostics(
        request_id=request_id,
        prompt_text=prompt_text,
        elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
        tool_progress_state=tool_progress_snapshot,
    )

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "request_id": request_id,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": tool_stats,
        "tool_invocations": tool_invocations,
        "prompt_introspection_fastpath": {"used_tool": used_tool, "enabled": True},
        "fastpath": {
            "name": "prompt_introspection",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
        "turn_execution_diagnostics": turn_execution_diagnostics,
    }
    llm_debug_info = _finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text=prompt_text,
        response_text=response_text,
        session_id=session_id,
        namespace=prompt_namespace,
        user_id=history_user_id or user_concept_id,
        org_id=org_concept_id,
    )

    if history_user_id:
        _add_chat_history_message(
            user_id=history_user_id,
            session_id=session_id,
            message={"role": "assistant", "content": response_text},
            llm_debug_data=llm_debug_info,
            namespace=prompt_namespace,
            organisation_concept_id=org_concept_id,
            role_in_org=role_in_org,
        )

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "prompt_introspection",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


def _maybe_handle_tool_inventory_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str | None,
    user_namespace: str | None,
    org_concept_id: str | None,
    role_in_org: str | None,
    history_user_id: str | None,
    session_id: str,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
    user_prompt_debug: dict,
    request_id: str | None = None,
    progress_scope_key: str | None = None,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
    prompt_namespace = user_namespace or user_concept_id

    response_text = None
    used_tool = False
    methods_snapshot = None

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            methods_snapshot = gateway.describe_methods()
            used_tool = True
            response_text = _format_tool_inventory(methods_snapshot)
        except Exception as exc:
            response_text = (
                f"Could not retrieve tool inventory from the internal gateway: {exc}"
            )
    else:
        response_text = ()

    current_turn_messages = [{"role": "user", "content": prompt_text}]
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(
        current_app.config.get("CONTEXT", [])
    )
    tool_progress_snapshot = _snapshot_tool_progress_for_request(
        progress_scope_key, request_id
    )
    turn_execution_diagnostics = _build_turn_execution_diagnostics(
        request_id=request_id,
        prompt_text=prompt_text,
        elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
        tool_progress_state=tool_progress_snapshot,
    )

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "request_id": request_id,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": None,
        "tool_invocations": (
            [
                {
                    "tool": "gateway.describe_methods",
                    "payload": {},
                    "duration_ms": None,
                    "direct_user_call": False,
                    "ok": bool(methods_snapshot is not None),
                }
            ]
            if used_tool
            else []
        ),
        "fastpath": {
            "name": "tool_inventory",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
        "turn_execution_diagnostics": turn_execution_diagnostics,
    }
    llm_debug_info = _finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text=prompt_text,
        response_text=(
            response_text if isinstance(response_text, str) else str(response_text)
        ),
        session_id=session_id,
        namespace=prompt_namespace,
        user_id=history_user_id or user_concept_id,
        org_id=org_concept_id,
    )

    if history_user_id:
        _add_chat_history_message(
            user_id=history_user_id,
            session_id=session_id,
            message={
                "role": "user",
                "content": prompt_text,
                "author_user_id": user_concept_id,
            },
            namespace=prompt_namespace,
            organisation_concept_id=org_concept_id,
            role_in_org=role_in_org,
        )
        _add_chat_history_message(
            user_id=history_user_id,
            session_id=session_id,
            message={"role": "assistant", "content": response_text},
            llm_debug_data=llm_debug_info,
            namespace=prompt_namespace,
            organisation_concept_id=org_concept_id,
            role_in_org=role_in_org,
        )
    else:
        stored_context = current_app.config.get("CONTEXT", [])
        stored_context.append({"role": "user", "content": prompt_text})
        stored_context.append({"role": "assistant", "content": response_text})
        current_app.config["CONTEXT"] = _limit_context_size(
            stored_context, max_messages=20
        )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "tool_inventory",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


def _maybe_handle_rag_status_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str,
    user_namespace: str | None,
    org_concept_id: str | None,
    role_in_org: str | None,
    history_user_id: str | None,
    session_id: str,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
    user_prompt_debug: dict,
    request_id: str | None = None,
    progress_scope_key: str | None = None,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
    import json as _json

    rag_namespace = user_namespace or user_concept_id

    tool_messages: list[dict] = []
    tool_invocations: list[dict] = []
    used_tool = False
    response_text = None

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            tool_result = gateway.invoke(
                "rag_get_status",
                {"namespace": rag_namespace},
            )
            payload = tool_result.payload
            duration_ms = getattr(tool_result, "duration_ms", None)
            used_tool = True

            tool_messages = [
                {
                    "role": "tool",
                    "content": _json.dumps(
                        {
                            "tool": "rag_get_status",
                            "status": "ok",
                            "duration_ms": duration_ms,
                            "payload": payload,
                        },
                        default=str,
                    ),
                }
            ]
            tool_invocations = [
                {
                    "tool": "rag_get_status",
                    "payload": {"namespace": rag_namespace},
                    "duration_ms": duration_ms,
                    "direct_user_call": False,
                }
            ]

            response_text = (
                "Here is your current RAG status (server-truth):\n\n"
                + _json.dumps(payload, indent=2, default=str)
            )
        except Exception as exc:
            response_text = f"I could not retrieve RAG status via internal tools: {exc}"
    else:
        response_text = "Internal MCP gateway is disabled; RAG status is unavailable."

    # Persist messages in history/context.
    if history_user_id:
        _add_chat_history_message(
            user_id=history_user_id,
            session_id=session_id,
            message={
                "role": "user",
                "content": prompt_text,
                "author_user_id": user_concept_id,
            },
            namespace=rag_namespace,
            organisation_concept_id=org_concept_id,
            role_in_org=role_in_org,
        )
    for tool_msg in _truncate_large_tool_results(
        tool_messages, max_tool_content_chars=5000
    ):
        if history_user_id:
            _add_chat_history_message(
                user_id=history_user_id,
                session_id=session_id,
                message=tool_msg,
                namespace=rag_namespace,
                organisation_concept_id=org_concept_id,
                role_in_org=role_in_org,
            )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    current_turn_messages = [{"role": "user", "content": prompt_text}] + tool_messages
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(current_app.config["CONTEXT"])
    tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None
    tool_progress_snapshot = _snapshot_tool_progress_for_request(
        progress_scope_key, request_id
    )
    turn_execution_diagnostics = _build_turn_execution_diagnostics(
        request_id=request_id,
        prompt_text=prompt_text,
        elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
        tool_progress_state=tool_progress_snapshot,
    )

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "request_id": request_id,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": tool_stats,
        "tool_invocations": tool_invocations,
        "fastpath": {
            "name": "rag_status",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
        "turn_execution_diagnostics": turn_execution_diagnostics,
    }
    llm_debug_info = _finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text=prompt_text,
        response_text=response_text,
        session_id=session_id,
        namespace=rag_namespace,
        user_id=history_user_id or user_concept_id,
        org_id=org_concept_id,
    )

    if history_user_id:
        _add_chat_history_message(
            user_id=history_user_id,
            session_id=session_id,
            message={"role": "assistant", "content": response_text},
            llm_debug_data=llm_debug_info,
            namespace=rag_namespace,
            organisation_concept_id=org_concept_id,
            role_in_org=role_in_org,
        )

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "rag_status",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


@von_bp.route("/onboard_new_member", methods=["POST"])
def onboard_new_member():
    """Start onboarding through the canonical durable workflow submission path."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body."}), 400

    member_name = data.get("member_name")
    if not isinstance(member_name, str) or not member_name.strip():
        return jsonify({"error": "No member name provided."}), 400
    member_name = member_name.strip()

    try:
        max_retries = _coerce_onboarding_max_retries(data.get("max_retries", 3))
    except ValueError:
        return (
            jsonify({"error": "max_retries must be an integer between 0 and 10."}),
            400,
        )

    inputs = _build_onboarding_inputs(member_name=member_name, request_payload=data)
    workflow_candidates = _resolve_onboarding_workflow_candidates(data)
    if not workflow_candidates:
        return (
            jsonify(
                {
                    "error": "No onboarding workflows configured or discoverable.",
                    "error_code": "onboarding_workflow_not_configured",
                }
            ),
            400,
        )

    try:
        user_concept_id = None
        try:
            from ...security.access_control import get_effective_user_concept_id

            user_concept_id = get_effective_user_concept_id()
        except Exception:
            user_concept_id = session.get("user_concept_id")

        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace_resolution = _resolve_generate_namespace_context(
            user_concept_id=user_concept_id,
            effective_context=effective,
            flask_session_snapshot=dict(session),
        )

        effective_org_id = _normalise_concept_id(
            effective.get("organisation_id") if isinstance(effective, dict) else None
        )
        requested_org_id = _normalise_concept_id(data.get("org_id"))
        org_id = effective_org_id or requested_org_id or "default"

        user_id = (
            _normalise_concept_id(user_concept_id)
            or _normalise_concept_id(data.get("user_id"))
            or _normalise_concept_id(session.get("user_concept_id"))
            or "anonymous"
        )

        from ...services.namespace_service import resolve_canonical_namespace

        requested_namespace = data.get("namespace")
        if not isinstance(requested_namespace, str) or not requested_namespace.strip():
            requested_namespace = None
        namespace = resolve_canonical_namespace(
            namespace_resolution.get("namespace") or requested_namespace,
            user_id,
            org_id if org_id != "default" else None,
        )
        if not isinstance(namespace, str) or not namespace.strip():
            return (
                jsonify(
                    {
                        "error": "namespace_resolution_failed",
                        "message": (
                            "Workflow launch namespace must be canonical or "
                            "derivable from authenticated user/org context."
                        ),
                    }
                ),
                400,
            )

        manager = get_instance_manager()
        attempt_payloads: list[dict[str, Any]] = []
        for workflow_id in workflow_candidates:
            submission = submit_verified_workflow_instance(
                manager=manager,
                workflow_id=workflow_id,
                user_id=user_id,
                org_id=org_id,
                namespace=namespace,
                inputs=inputs,
                max_retries=max_retries,
            )
            submission_payload = submission.to_dict()
            attempt_payloads.append(submission_payload)
            if submission.success:
                return (
                    jsonify(
                        {
                            "message": f"Onboarding workflow started for {member_name}.",
                            "member_name": member_name,
                            "selected_workflow_id": workflow_id,
                            "candidate_workflow_ids": workflow_candidates,
                            "attempt_count": len(attempt_payloads),
                            **submission_payload,
                        }
                    ),
                    200,
                )

        return (
            jsonify(
                {
                    "error": "No runnable onboarding workflow is currently available.",
                    "error_code": "onboarding_workflow_not_runnable",
                    "member_name": member_name,
                    "candidate_workflow_ids": workflow_candidates,
                    "attempts": attempt_payloads,
                }
            ),
            400,
        )
    except Exception as exc:
        current_app.logger.exception(
            "Failed to start onboarding workflow for member '%s': %s",
            member_name,
            exc,
        )
        return (
            jsonify(
                {
                    "error": "Error during onboarding workflow submission.",
                    "detail": str(exc),
                }
            ),
            500,
        )


@von_bp.route("/update_model", methods=["POST"])
def update_model():
    data = request.get_json()
    new_model = data.get("model")

    if new_model:
        current_app.config["MODEL"] = new_model
        print(f"Model updated to: {new_model}")  # Add server-side logging
        return jsonify({"message": f"Model updated to {new_model}"}), 200
    return jsonify({"error": "No model provided."}), 400


@von_bp.route("/")
def serve_page():
    """Serve the main chat interface (HTML file)."""
    try:
        # Ensure template changes (e.g., recent fixes) are picked up even in production mode
        jenv = getattr(current_app, "jinja_env", None)
        cache = getattr(jenv, "cache", None)
        clear_fn = getattr(cache, "clear", None)
        if callable(clear_fn):
            clear_fn()
    except Exception:
        pass  # Defensive: don't block page serving if cache clear fails
    return render_template("von_interface.html")


@von_bp.route("/workflow-studio")
def serve_workflow_studio_page():
    """Serve the independent workflow studio surface."""
    return render_template("workflow_studio.html")


def _submit_generate_background_request(
    *,
    app: Any,
    request_data: Mapping[str, Any] | None,
    request_headers: Mapping[str, Any],
    session_snapshot: Mapping[str, Any],
    request_id: str,
):
    background_payload = dict(request_data) if isinstance(request_data, Mapping) else {}
    background_payload["background"] = False
    background_payload["client_request_id"] = request_id
    background_payload["background_task_id"] = request_id
    background_payload["background_progress"] = True

    background_headers: dict[str, str] = {}
    for header_name in (
        "X-Von-Window-Session",
        "X-User-Concept-ID",
        "X-User-Client-ID",
    ):
        header_value = request_headers.get(header_name)
        if isinstance(header_value, str) and header_value.strip():
            background_headers[header_name] = header_value.strip()

    background_session_id = (
        str(session_snapshot.get("session_id")).strip()
        if session_snapshot.get("session_id")
        else None
    )
    background_user_id = (
        str(session_snapshot.get("user_concept_id")).strip()
        if session_snapshot.get("user_concept_id")
        else (
            str(session_snapshot.get("user_id")).strip()
            if session_snapshot.get("user_id")
            else None
        )
    )

    def _run_generate_request_in_background() -> dict[str, Any]:
        with app.test_request_context(
            "/von/generate",
            method="POST",
            json=background_payload,
            headers=background_headers,
        ):
            session.clear()
            session.update(session_snapshot)
            session.modified = True
            return _normalise_background_generate_result(generate())

    task_status = background_task_registry.submit_task(
        task_id=request_id,
        callable=_run_generate_request_in_background,
        progress_callback=None,
        session_id=background_session_id,
        user_id=background_user_id,
    )

    return (
        jsonify(
            {
                "background": True,
                "task_id": request_id,
                "status": task_status.status,
                "message": "Task submitted for background execution",
                "status_url": f"/von/api/task/status/{request_id}",
                "result_url": f"/von/api/task/result/{request_id}",
            }
        ),
        202,
    )


def _persist_terminal_failure_turn_record_best_effort(
    *,
    local_vars: Mapping[str, Any],
    terminal_status: str,
    error_text: str | None,
    error_class: str | None,
    llm_debug_info_override: dict[str, Any] | None = None,
    progress_snapshot: Mapping[str, Any] | None = None,
) -> None:
    """Persist a turn record for a failed/cancelled generate turn.

    JVNAUTOSCI-2502: failed turns previously persisted nothing, so the turns
    that most need diagnostics were invisible to telemetry. Reads the generate
    route's locals defensively because failure can occur at any stage.
    """

    try:
        request_id = local_vars.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return
        debug_payload = llm_debug_info_override
        if debug_payload is None:
            raw_debug = local_vars.get("llm_debug_info")
            debug_payload = raw_debug if isinstance(raw_debug, dict) else None
        from ...services.turn_execution_record_service import (
            persist_failed_turn_execution_record,
        )

        persist_failed_turn_execution_record(
            request_id=request_id,
            terminal_status=terminal_status,
            error_text=error_text,
            error_class=error_class,
            session_id=local_vars.get("session_id"),
            namespace=local_vars.get("user_namespace"),
            user_id=local_vars.get("user_concept_id"),
            org_id=local_vars.get("org_concept_id"),
            prompt_text=local_vars.get("prompt_text"),
            llm_debug_info=debug_payload,
            progress_snapshot=progress_snapshot,
        )
    except Exception as exc:
        try:
            current_app.logger.warning(
                "Terminal-failure turn record persistence failed: %s", exc
            )
        except Exception:
            pass


@von_bp.route("/generate", methods=["POST"])
def generate():  # pyright: ignore[reportGeneralTypeIssues]
    """Handle text generation requests."""
    data = request.get_json()
    prompt_text = data.get("prompt", "")

    raw_skip_buttonify = data.get("skip_buttonify")
    if isinstance(raw_skip_buttonify, str):
        skip_buttonify = raw_skip_buttonify.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    else:
        skip_buttonify = bool(raw_skip_buttonify)

    client_request_id = data.get("client_request_id")
    if (
        isinstance(client_request_id, str)
        and client_request_id.strip()
        and len(client_request_id) <= 200
    ):
        request_id = client_request_id.strip()
    else:
        request_id = str(uuid.uuid4())

    if not prompt_text:
        return jsonify({"error": "No prompt provided."}), 400

    # JVNAUTOSCI-1038: Background execution mode
    background_mode = bool(data.get("background", False))
    background_task_id = None
    raw_background_task_id = data.get("background_task_id")
    if (
        isinstance(raw_background_task_id, str)
        and raw_background_task_id.strip()
        and len(raw_background_task_id.strip()) <= 200
    ):
        candidate_background_task_id = raw_background_task_id.strip()
        if candidate_background_task_id == request_id:
            background_task_id = candidate_background_task_id

    presenter_mode_requested = bool(data.get("presenter_mode"))

    # JVNAUTOSCI-2130: Forward the thinking-card display mode so the orchestrator
    # can produce a partial-progress summary (instead of the canned "couldn't
    # complete that request" fallback) when the user has explicitly opted into
    # expert/debug detail.
    _raw_thinking_card_mode = data.get("thinking_card_mode")
    if isinstance(_raw_thinking_card_mode, str):
        _normalised_thinking_card_mode = _raw_thinking_card_mode.strip().lower()
        if _normalised_thinking_card_mode in {"default", "expert", "debug"}:
            thinking_card_mode = _normalised_thinking_card_mode
        else:
            thinking_card_mode = "default"
    else:
        thinking_card_mode = "default"

    if background_mode:
        return _submit_generate_background_request(
            app=current_app._get_current_object(),
            request_data=data if isinstance(data, Mapping) else None,
            request_headers=request.headers,
            session_snapshot=dict(session),
            request_id=request_id,
        )

    request_start_perf = time.perf_counter()
    turn_timing_recorder = TurnTimingRecorder(request_id=request_id)
    workflow_discovery_result = None
    progress_heartbeat_stop_event: threading.Event | None = None
    progress_heartbeat_thread: threading.Thread | None = None
    interaction_timestamp_utc = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    progress_scope_key = _get_tool_progress_bootstrap_scope_key()
    progress_mirror_scope_keys: list[str] = []
    request_window_session_id = request.headers.get(_WINDOW_SESSION_HEADER_NAME)
    show_tool_use_progress = False
    try:
        show_tool_use_progress = bool(get_show_tool_use_during_thinking())
    except Exception:
        show_tool_use_progress = False
    if background_task_id is not None:
        show_tool_use_progress = False
    progress_updates_enabled = show_tool_use_progress or background_task_id is not None

    def _emit_generate_progress(update: Mapping[str, Any] | None) -> None:
        payload = dict(update) if isinstance(update, Mapping) else {"status": "unknown"}
        payload.setdefault("request_id", request_id)

        if background_task_id is not None:
            try:
                update_background_progress = getattr(
                    background_task_registry, "update_progress", None
                )
                if callable(update_background_progress):
                    update_background_progress(background_task_id, payload)
            except Exception:
                pass

        if not show_tool_use_progress:
            return

        seen_scope_keys: set[str] = set()
        for target_scope_key in [progress_scope_key, *progress_mirror_scope_keys]:
            clean_scope_key = _progress_str(target_scope_key)
            if not clean_scope_key or clean_scope_key in seen_scope_keys:
                continue
            seen_scope_keys.add(clean_scope_key)
            _set_tool_progress(clean_scope_key, request_id, dict(payload))

    def _emit_context_setup_progress(
        *,
        subtask: str,
        result_summary: str,
        status: str = "thinking",
        phase_label: str = "Building context",
        **extra: Any,
    ) -> None:
        if not progress_updates_enabled:
            return
        payload: dict[str, Any] = {
            "status": status,
            "stage": "context_build",
            "phase": "context_build",
            "phase_label": phase_label,
            "workflow_task": "context_build",
            "goal_label": progress_goal_label,
            "request_id": request_id,
            "subtask": subtask,
            "result_summary": result_summary,
        }
        payload.update(extra)
        _emit_generate_progress(payload)

    def _is_background_cancellation_requested() -> bool:
        if background_task_id is None:
            return False
        try:
            is_requested = getattr(
                background_task_registry,
                "is_cancellation_requested",
                None,
            )
            if callable(is_requested):
                return bool(is_requested(background_task_id))
        except Exception:
            return False
        return False

    def _check_background_cancellation(subtask: str) -> None:
        if not _is_background_cancellation_requested():
            return
        _emit_context_setup_progress(
            subtask=subtask,
            status="cancelled",
            phase_label="Cancelling",
            result_summary="Cancellation requested while preparing the generate request.",
        )
        raise CancellationRequested(task_id=background_task_id)

    progress_goal_label = _build_progress_goal_label(prompt_text=prompt_text)
    if show_tool_use_progress:
        _register_tool_progress_scope_aliases(
            request_id=request_id,
            primary_scope_key=progress_scope_key,
            mirror_scope_keys=progress_mirror_scope_keys,
            window_session_id=request_window_session_id,
            anonymous_session_id=_progress_str(session.get("tool_progress_scope")),
        )

    if progress_updates_enabled:
        _emit_generate_progress(
            _build_request_initialising_tool_progress_payload(
                request_id=request_id,
                goal_label=progress_goal_label,
            ),
        )

    _emit_context_setup_progress(
        subtask="request payload fields",
        result_summary="Parsing generate request metadata before session/context lookup.",
    )

    # Get user/org context from request body (sent by frontend from localStorage)
    request_user_id = data.get("user_id")
    request_org_id = data.get("org_id")
    request_language = data.get("language", "en-NZ")
    request_conversation_session_id_raw = data.get("conversation_session_id")
    request_conversation_session_id = None
    if isinstance(request_conversation_session_id_raw, str):
        request_conversation_session_id = (
            request_conversation_session_id_raw.strip() or None
        )
    elif request_conversation_session_id_raw is not None:
        return (
            jsonify(
                {
                    "error": "invalid_conversation_session_id",
                    "detail": "conversation_session_id must be a string.",
                }
            ),
            400,
        )
    request_gmail_profile_raw = data.get("gmail_profile")
    request_gmail_profile = None
    if isinstance(request_gmail_profile_raw, str):
        request_gmail_profile = request_gmail_profile_raw.strip() or None
    elif request_gmail_profile_raw is not None:
        return (
            jsonify(
                {
                    "error": "invalid_gmail_profile",
                    "detail": "gmail_profile must be a string.",
                }
            ),
            400,
        )

    if request_gmail_profile:
        from ...integrations.google.gmail_service import list_profile_ids_from_env

        available_profiles = list_profile_ids_from_env()
        if not available_profiles:
            return (
                jsonify(
                    {
                        "error": "gmail_profiles_not_configured",
                        "detail": "No Gmail profiles are configured on the server.",
                    }
                ),
                400,
            )
        if request_gmail_profile not in available_profiles:
            return (
                jsonify(
                    {
                        "error": "unknown_gmail_profile",
                        "detail": "gmail_profile is not configured on the server.",
                        "available_profiles": available_profiles,
                    }
                ),
                400,
            )

    request_turn_memory_context = None
    request_turn_memory_context_raw = data.get("turn_memory_context")
    if isinstance(request_turn_memory_context_raw, Mapping):
        request_turn_memory_context = dict(request_turn_memory_context_raw)
    elif request_turn_memory_context_raw is not None:
        return (
            jsonify(
                {
                    "error": "invalid_turn_memory_context",
                    "detail": "turn_memory_context must be an object.",
                }
            ),
            400,
        )

    request_turn_expected_outcome_contract = None
    request_turn_expected_outcome_contract_raw = data.get(
        "turn_expected_outcome_contract"
    )
    if request_turn_expected_outcome_contract_raw is None:
        request_turn_expected_outcome_contract_raw = data.get(
            "turn_expected_outcome_contract_state"
        )
    if isinstance(request_turn_expected_outcome_contract_raw, Mapping):
        request_turn_expected_outcome_contract = {
            str(key): value
            for key, value in request_turn_expected_outcome_contract_raw.items()
            if isinstance(key, str)
        }
    elif request_turn_expected_outcome_contract_raw is not None:
        return (
            jsonify(
                {
                    "error": "invalid_turn_expected_outcome_contract",
                    "detail": "turn_expected_outcome_contract must be an object.",
                }
            ),
            400,
        )

    _emit_context_setup_progress(
        subtask="flask session user lookup",
        result_summary="Reading authenticated user id from the Flask session.",
    )
    user_concept_id = session.get("user_concept_id")
    _emit_context_setup_progress(
        subtask="context cache lookup",
        result_summary="Reading in-memory request context before authentication resolution.",
    )
    context = current_app.config.get("CONTEXT", [])

    # REFACTORING_NOTE: Use the new factory to get the correct client and model
    # Get user and org context for per-user/org LLM settings
    _emit_context_setup_progress(
        subtask="authentication context",
        result_summary="Resolving authenticated user and window-session context.",
    )
    _check_background_cancellation("authentication context")
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()

        # SECURITY: Do NOT trust client-provided user_id - require proper authentication
        # User must be authenticated via:
        # 1. Server-side session (populated during login flow)
        # 2. Validated headers (X-User-Concept-ID with concept validation)
        # If no authenticated user, user_concept_id will be None and RAG tools will be unavailable
        if not user_concept_id:
            current_app.logger.info(
                "[AUTH] No authenticated user for this request. RAG and user-scoped tools will be unavailable. "
                "Client-provided user_id '%s' is ignored for security.",
                request_user_id or "(none)",
            )

        # JVNAUTOSCI-1011: Use window session context if available
        effective = get_effective_context(
            request_window_session_id, dict(session), user_concept_id
        )
        org_concept_id = effective.get("organisation_id")

        # Store user_concept_id in session for history tracking
        if user_concept_id:
            session["user_concept_id"] = user_concept_id
    except Exception:
        user_concept_id = None
        org_concept_id = None
        effective = {}

    _check_background_cancellation("authentication context")

    if show_tool_use_progress:
        _register_tool_progress_scope_aliases(
            request_id=request_id,
            primary_scope_key=progress_scope_key,
            mirror_scope_keys=progress_mirror_scope_keys,
            user_concept_id=user_concept_id,
            window_session_id=request_window_session_id,
            anonymous_session_id=_progress_str(session.get("tool_progress_scope")),
        )

    role_in_org = effective.get("role") if isinstance(effective, dict) else None

    _emit_context_setup_progress(
        subtask="namespace resolution",
        result_summary="Resolving namespace and organisation scope for the turn.",
    )
    _check_background_cancellation("namespace resolution")
    namespace_resolution = _resolve_generate_namespace_context(
        user_concept_id=user_concept_id,
        effective_context=effective if isinstance(effective, dict) else {},
        flask_session_snapshot=dict(session),
    )
    user_namespace = namespace_resolution.get("namespace")
    namespace_source = namespace_resolution.get("namespace_source") or "missing"
    namespace_report: dict[str, object] = {
        "authenticated": bool(user_concept_id),
        "user_concept_id": user_concept_id,
        "organisation_concept_id": org_concept_id,
        "namespace": user_namespace,
        "namespace_source": namespace_source,
        "effective_context_source": namespace_resolution.get(
            "effective_context_source"
        ),
        "effective_context_namespace": namespace_resolution.get(
            "effective_context_namespace"
        ),
        "session_namespace": namespace_resolution.get("session_namespace"),
        "candidates": namespace_resolution.get("candidates") or [],
        "mismatch_detected": bool(namespace_resolution.get("mismatch_detected")),
        "org_scope_preferred": bool(namespace_resolution.get("org_scope_preferred")),
    }
    if isinstance(namespace_resolution.get("mismatch_reason"), str):
        namespace_report["mismatch_reason"] = namespace_resolution.get(
            "mismatch_reason"
        )
    if (
        isinstance(user_namespace, str)
        and user_namespace.strip()
        and user_namespace != namespace_resolution.get("effective_context_namespace")
    ):
        namespace_report["selected_namespace_differs_from_effective_context"] = True
    _record_namespace_isolation_observation(
        flow="/von/generate",
        namespace=user_namespace if isinstance(user_namespace, str) else None,
        namespace_source=(
            namespace_source if isinstance(namespace_source, str) else "missing"
        ),
        user_concept_id=user_concept_id if isinstance(user_concept_id, str) else None,
        organisation_concept_id=(
            org_concept_id if isinstance(org_concept_id, str) else None
        ),
        mismatch_detected=bool(namespace_report.get("mismatch_detected")),
        details={
            "effective_context_source": namespace_report.get(
                "effective_context_source"
            ),
            "effective_context_namespace": namespace_report.get(
                "effective_context_namespace"
            ),
            "session_namespace": namespace_report.get("session_namespace"),
        },
    )
    if not user_namespace:
        current_app.logger.warning(
            "[NAMESPACE] No effective namespace resolved for user_concept_id=%s",
            user_concept_id,
        )

    _emit_context_setup_progress(
        subtask="conversation session",
        result_summary="Ensuring a conversation session exists for this replay turn.",
    )
    _check_background_cancellation("conversation session")
    session_id, created_conversation_session_name, created_conversation_session = (
        _ensure_generate_conversation_session(
            request_conversation_session_id=request_conversation_session_id,
            effective_context=effective if isinstance(effective, Mapping) else {},
            user_concept_id=user_concept_id,
            user_namespace=user_namespace if isinstance(user_namespace, str) else None,
            org_concept_id=(
                org_concept_id if isinstance(org_concept_id, str) else None
            ),
            role_in_org=role_in_org if isinstance(role_in_org, str) else None,
            window_session_id=request_window_session_id,
        )
    )
    _check_background_cancellation("conversation session")

    _emit_context_setup_progress(
        subtask="shared conversation owner",
        result_summary="Resolving shared-conversation ownership before chat-history lookup.",
    )
    history_owner_user_id, shared_invite = _resolve_shared_conversation_owner(
        user_concept_id=user_concept_id, session_id=session_id
    )
    history_user_id = history_owner_user_id or user_concept_id
    _check_background_cancellation("shared conversation owner")

    if history_user_id:
        if progress_updates_enabled:
            _emit_generate_progress(
                {
                    "status": "thinking",
                    "phase": "context_build",
                    "phase_label": "Building context",
                    "goal_label": progress_goal_label,
                    "request_id": request_id,
                    "subtask": "chat-history lookup",
                    "result_summary": "Loading chat history for the active session.",
                },
            )
        _check_background_cancellation("chat-history lookup")
        _chat_history_start_perf = time.perf_counter()
        try:
            if (
                shared_invite
                and history_owner_user_id
                and user_concept_id
                and history_owner_user_id != user_concept_id
            ):
                shared_invite_org_concept_id = (
                    _normalise_concept_id(shared_invite.get("organisation_concept_id"))
                    if isinstance(shared_invite, Mapping)
                    else None
                )
                owner_history_namespace = _derive_namespace_for_user_org(
                    history_owner_user_id, shared_invite_org_concept_id
                ) or chat_history_service.resolve_chat_history_namespace(
                    history_owner_user_id
                )
                invitee_history_namespace = (
                    user_namespace
                    if isinstance(user_namespace, str) and user_namespace.strip()
                    else _derive_namespace_for_user_org(
                        user_concept_id, shared_invite_org_concept_id
                    )
                ) or chat_history_service.resolve_chat_history_namespace(
                    user_concept_id
                )
                owner_history = chat_history_service.get_chat_history(
                    history_owner_user_id,
                    session_id,
                    namespace=owner_history_namespace,
                )
                invitee_history = chat_history_service.get_chat_history(
                    user_concept_id,
                    session_id,
                    namespace=invitee_history_namespace,
                )
                owner_history = _apply_default_author(
                    owner_history, history_owner_user_id
                )
                invitee_history = _apply_default_author(
                    invitee_history, user_concept_id
                )
                context = _merge_shared_histories(owner_history, invitee_history)
            else:
                context = chat_history_service.get_chat_history(
                    history_user_id,
                    session_id,
                    namespace=(
                        user_namespace
                        if isinstance(user_namespace, str) and user_namespace.strip()
                        else None
                    ),
                )
            _check_background_cancellation("chat-history lookup")
        except Exception as chat_history_exc:
            if not chat_history_service.is_transient_chat_history_error(
                chat_history_exc
            ):
                raise
            current_app.logger.warning(
                "/von/generate continuing with empty prior context after transient "
                "chat-history lookup failure for session_id=%s request_id=%s: %s",
                session_id,
                request_id,
                chat_history_exc,
                exc_info=True,
            )
            context = []
            namespace_report["chat_history_context_degraded"] = True
            namespace_report["chat_history_context_degraded_reason"] = str(
                chat_history_exc
            )[:300]
            if progress_updates_enabled:
                _emit_generate_progress(
                    {
                        "status": "thinking",
                        "phase": "context_build",
                        "phase_label": "Building context",
                        "goal_label": progress_goal_label,
                        "request_id": request_id,
                        "subtask": "chat-history lookup degraded",
                        "result_summary": (
                            "Chat history is temporarily unavailable; continuing "
                            "with an empty prior conversation context."
                        ),
                    },
                )
        finally:
            _chat_history_elapsed_ms = (
                time.perf_counter() - _chat_history_start_perf
            ) * 1000.0
            if _chat_history_elapsed_ms > 5000:
                current_app.logger.warning(
                    "[CHAT_HISTORY_SLOW] Chat-history lookup took %.0fms "
                    "for session_id=%s request_id=%s",
                    _chat_history_elapsed_ms,
                    session_id,
                    request_id,
                )

    # ------------------------------------------------------------------
    # JVNAUTOSCI-1423: Persist user message to chat history immediately,
    # BEFORE orchestrator/LLM work begins.  This guarantees the user's
    # message is recorded even if the downstream turn crashes or stalls.
    # Downstream paths must NOT re-persist the user message. This early
    # durability write must also avoid synchronous best-effort RAG indexing,
    # otherwise embeddings/quota delays can stall the browser turn before
    # workflow selection even begins.
    # ------------------------------------------------------------------
    _user_message_persisted_early = False
    if history_user_id:
        _emit_context_setup_progress(
            subtask="early user-message persist",
            result_summary="Persisting the user message before workflow selection.",
        )
        _check_background_cancellation("early user-message persist")
        try:
            _add_chat_history_message(
                user_id=history_user_id,
                session_id=session_id,
                message={
                    "role": "user",
                    "content": prompt_text,
                    "author_user_id": user_concept_id,
                },
                namespace=user_namespace,
                organisation_concept_id=org_concept_id,
                role_in_org=role_in_org,
                skip_rag_indexing=True,
            )
            _user_message_persisted_early = True
        except Exception as early_persist_exc:
            current_app.logger.warning(
                "[EARLY_PERSIST] Failed to persist user message early "
                "for session_id=%s request_id=%s: %s",
                session_id,
                request_id,
                early_persist_exc,
            )
            _check_background_cancellation("early user-message persist")

    if progress_updates_enabled:
        try:
            max_calls = int(get_internal_mcp_max_tool_invocations())
        except Exception:
            max_calls = 0
        try:
            batch_cap = int(get_internal_mcp_tool_batch_cap())
        except Exception:
            batch_cap = 4
        _emit_generate_progress(
            {
                "status": "thinking",
                "phase": "context_build",
                "phase_label": "Building context",
                "goal_label": progress_goal_label,
                "request_id": request_id,
                "tool": None,
                "batch_size": None,
                "subtask": "context assembly",
                "result_summary": (
                    "Preparing prompt context and workflow inputs for the turn."
                ),
                "tool_calls_done": 0,
                "tool_calls_cap": max_calls,
                "tool_calls_remaining": max(0, max_calls),
                "tool_batch_cap": batch_cap,
            },
        )
    elif not show_tool_use_progress:
        progress_goal_label = None

    _emit_context_setup_progress(
        subtask="model client selection",
        result_summary="Resolving the requested model and local provider client.",
    )
    _check_background_cancellation("model client selection")
    model_name, explicit_client_type = _resolve_generate_requested_model(
        data,
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
        configured_model=current_app.config.get("MODEL"),
    )

    try:
        llm_client = get_llm_client(
            client_type=explicit_client_type,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
        )
    except Exception as e:
        if progress_updates_enabled:
            _emit_generate_progress(
                {
                    "status": "error",
                    "phase": "context_build",
                    "phase_label": "Error",
                    "request_id": request_id,
                    "error": f"Could not get LLM client: {e}",
                    "result_summary": (
                        "Could not initialise the configured LLM client for this turn."
                    ),
                },
            )
        return jsonify({"error": f"Could not get LLM client: {e}"}), 500
    _check_background_cancellation("model client selection")

    if show_tool_use_progress and not background_mode:
        progress_heartbeat_stop_event, progress_heartbeat_thread = (
            _start_tool_progress_heartbeat(
                [progress_scope_key, *progress_mirror_scope_keys], request_id
            )
        )

    try:
        # Build system message with user and organization context from request
        system_message_parts = []

        # ---------------------------------------------------------
        # JVNAUTOSCI-797: user-specific system prompt from Vontology
        # ---------------------------------------------------------
        dynamic_instructions = None
        narration_prompt_text = None
        narration_prompt_fragments = []
        screen_prompt_text = None
        screen_prompt_fragments = []
        user_prompt_debug = {
            "effective_user_concept_id": user_concept_id,
            "loaded": False,
            "chars": 0,
            "prompt_concept_ids": [],
            "behaviour_prompt_concept_ids": [],
            "narration_prompt_concept_ids": [],
            "screen_prompt_concept_ids": [],
        }
        if user_concept_id:
            _emit_context_setup_progress(
                subtask="user prompt fragments",
                result_summary="Loading user-specific prompt fragments from Vontology.",
            )
            _check_background_cancellation("user prompt fragments")
            try:
                from ...services.chat_auxiliary_prompt_service import (
                    get_user_specific_prompt_fragments,
                )

                behaviour_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=(
                        "#V#von_chat_behaviour_prompt",
                        "#V#von_chat_behavior_prompt",
                        "#V#von_llm_prompt",
                    ),
                )
                narration_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=("#V#von_chat_narration_prompt",),
                )
                screen_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=("#V#von_chat_screen_content_prompt",),
                )

                user_prompt_debug["behaviour_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in behaviour_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                user_prompt_debug["narration_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in narration_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                user_prompt_debug["screen_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in screen_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                # Backwards-compatible field name used by the UI/debug tools.
                user_prompt_debug["prompt_concept_ids"] = list(
                    user_prompt_debug["behaviour_prompt_concept_ids"]
                )

                prompt_texts = []
                for frag in behaviour_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    prompt_texts.append(content)
                dynamic_instructions = "\n\n".join(
                    text.strip() for text in prompt_texts if text and text.strip()
                )
                dynamic_instructions = (
                    dynamic_instructions.strip() if dynamic_instructions else None
                )

                narration_texts = []
                for frag in narration_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    narration_texts.append(content)
                narration_prompt_text = "\n\n".join(
                    text.strip() for text in narration_texts if text and text.strip()
                )
                narration_prompt_text = (
                    narration_prompt_text.strip() if narration_prompt_text else None
                )

                screen_texts = []
                for frag in screen_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    screen_texts.append(content)
                screen_prompt_text = "\n\n".join(
                    text.strip() for text in screen_texts if text and text.strip()
                )
                screen_prompt_text = (
                    screen_prompt_text.strip() if screen_prompt_text else None
                )

                if dynamic_instructions:
                    user_prompt_debug["loaded"] = True
                    user_prompt_debug["chars"] = len(dynamic_instructions)
                if dynamic_instructions:
                    current_app.logger.info(
                        "[CHAT_PROMPT] Loaded %d chars of user-specific prompt for %s",
                        len(dynamic_instructions),
                        user_concept_id,
                    )
            except Exception as e:
                current_app.logger.warning(
                    "[CHAT_PROMPT] Failed loading user-specific prompt for %s: %s",
                    user_concept_id,
                    e,
                )
                user_prompt_debug["error"] = str(e)
            _check_background_cancellation("user prompt fragments")

        deterministic_introspection_enabled = _deterministic_introspection_enabled()

        if deterministic_introspection_enabled and user_concept_id:
            if _is_prompt_introspection_question(prompt_text):
                return _maybe_handle_prompt_introspection_fastpath(
                    prompt_text=prompt_text,
                    user_concept_id=user_concept_id,
                    user_namespace=user_namespace,
                    org_concept_id=org_concept_id,
                    role_in_org=role_in_org,
                    history_user_id=history_user_id,
                    session_id=session_id,
                    auxiliary_system_prompt=dynamic_instructions,
                    user_prompt_debug=user_prompt_debug,
                    context=context,
                    interaction_timestamp_utc=interaction_timestamp_utc,
                    model_name=model_name or "unknown",
                    request_start_perf=request_start_perf,
                    request_id=request_id,
                    progress_scope_key=progress_scope_key,
                )

            if _is_rag_status_question(prompt_text):
                return _maybe_handle_rag_status_fastpath(
                    prompt_text=prompt_text,
                    user_concept_id=user_concept_id,
                    user_namespace=user_namespace,
                    org_concept_id=org_concept_id,
                    role_in_org=role_in_org,
                    history_user_id=history_user_id,
                    session_id=session_id,
                    context=context,
                    interaction_timestamp_utc=interaction_timestamp_utc,
                    model_name=model_name or "unknown",
                    request_start_perf=request_start_perf,
                    user_prompt_debug=user_prompt_debug,
                    request_id=request_id,
                    progress_scope_key=progress_scope_key,
                )

        if deterministic_introspection_enabled and _is_tool_introspection_question(
            prompt_text
        ):
            return _maybe_handle_tool_inventory_fastpath(
                prompt_text=prompt_text,
                user_concept_id=user_concept_id,
                user_namespace=user_namespace,
                org_concept_id=org_concept_id,
                role_in_org=role_in_org,
                history_user_id=history_user_id,
                session_id=session_id,
                context=context,
                interaction_timestamp_utc=interaction_timestamp_utc,
                model_name=model_name or "unknown",
                request_start_perf=request_start_perf,
                user_prompt_debug=user_prompt_debug,
                request_id=request_id,
                progress_scope_key=progress_scope_key,
            )

        effective_request_user_id = (
            user_concept_id
            if isinstance(user_concept_id, str) and user_concept_id.strip()
            else request_user_id
        )
        effective_request_org_id = (
            org_concept_id
            if isinstance(org_concept_id, str) and org_concept_id.strip()
            else request_org_id
        )

        # Try to get user name from concept if user_id provided
        if effective_request_user_id:
            _emit_context_setup_progress(
                subtask="user concept lookup",
                result_summary="Resolving the authenticated user's concept label.",
            )
            _check_background_cancellation("user concept lookup")
            try:
                from ...services.concept_service import get_concept_by_concept_id

                user_concept = get_concept_by_concept_id(effective_request_user_id)
                if user_concept:
                    user_name = user_concept.get("name") or effective_request_user_id
                    system_message_parts.append(
                        f"Current user: {user_name} ({effective_request_user_id})"
                    )
                    current_app.logger.info(
                        f"User context: {user_name} ({effective_request_user_id})"
                    )
                else:
                    system_message_parts.append(
                        f"Current user ID: {effective_request_user_id}"
                    )
            except Exception as e:
                current_app.logger.warning(
                    f"Could not fetch user concept {effective_request_user_id}: {e}"
                )
                system_message_parts.append(
                    f"Current user ID: {effective_request_user_id}"
                )
            _check_background_cancellation("user concept lookup")

        # Try to get organization name from concept if org_id provided
        if effective_request_org_id:
            _emit_context_setup_progress(
                subtask="organisation concept lookup",
                result_summary="Resolving the active organisation concept label.",
            )
            _check_background_cancellation("organisation concept lookup")
            try:
                from ...services.concept_service import get_concept_by_concept_id

                org_concept = get_concept_by_concept_id(effective_request_org_id)
                if org_concept:
                    org_name = org_concept.get("name") or effective_request_org_id
                    system_message_parts.append(
                        f"Organization: {org_name} ({effective_request_org_id})"
                    )
                    current_app.logger.info(
                        f"Organization context: {org_name} ({effective_request_org_id})"
                    )
                else:
                    system_message_parts.append(
                        f"Organization ID: {effective_request_org_id}"
                    )
            except Exception as e:
                current_app.logger.warning(
                    f"Could not fetch org concept {effective_request_org_id}: {e}"
                )
                system_message_parts.append(
                    f"Organization ID: {effective_request_org_id}"
                )
            _check_background_cancellation("organisation concept lookup")

        # Add language preference if provided
        if request_language and request_language != "en-NZ":
            system_message_parts.append(f"Language preference: {request_language}")

        # Create enhanced context with system message if we have user/org info
        enhanced_context = context.copy()
        if system_message_parts:
            # Create system message
            system_message = "You are Von, an AI assistant. " + " | ".join(
                system_message_parts
            )

            # Insert system message at the beginning if not already present
            if not enhanced_context or enhanced_context[0].get("role") != "system":
                enhanced_context.insert(
                    0, {"role": "system", "content": system_message}
                )
            else:
                # Update existing system message to include user/org context
                existing_system = enhanced_context[0]["content"]
                if not any(part in existing_system for part in system_message_parts):
                    enhanced_context[0][
                        "content"
                    ] = f"{existing_system} | {' | '.join(system_message_parts)}"

        # Ensure user-specific system prompt is included even when the orchestrator
        # is disabled/unavailable.
        if dynamic_instructions:
            user_prompt_message = {
                "role": "system",
                "content": "USER-SPECIFIC SYSTEM PROMPT (from Vontology):\n"
                + dynamic_instructions,
            }
            if not enhanced_context or enhanced_context[0].get("role") != "system":
                enhanced_context.insert(0, user_prompt_message)
            else:
                enhanced_context.insert(1, user_prompt_message)

        # ---------------------------------------------------------
        # JVNAUTOSCI-894: Presenter-mode response protocol
        # ---------------------------------------------------------
        if presenter_mode_requested:
            # Include a small client-reported timing hint for narration generation.
            # (Non-authoritative; used only for guidance.)
            timing_hint = None
            try:
                from ...services.client_capabilities_service import (
                    get_client_capabilities_snapshot,
                )

                snapshot = get_client_capabilities_snapshot()
                speech = (
                    snapshot.get("speech_synthesis")
                    if isinstance(snapshot, dict)
                    else None
                )
                speech = speech if isinstance(speech, dict) else {}
                raw_settings = speech.get("settings")
                settings = raw_settings if isinstance(raw_settings, dict) else {}

                preferred = settings.get("preferred_speaking_seconds")
                maximum = settings.get("max_speaking_seconds")

                try:
                    preferred_int = int(preferred) if preferred is not None else None
                except Exception:
                    preferred_int = None

                try:
                    maximum_int = int(maximum) if maximum is not None else None
                except Exception:
                    maximum_int = None

                if preferred_int is not None:
                    preferred_int = max(1, min(preferred_int, 600))
                if maximum_int is not None:
                    maximum_int = max(1, min(maximum_int, 600))

                effective_preferred = preferred_int
                if preferred_int is not None and maximum_int is not None:
                    effective_preferred = min(preferred_int, maximum_int)

                if effective_preferred is not None or maximum_int is not None:
                    timing_hint = (
                        "Speech timing hint (client-reported, non-authoritative): "
                        f"preferred_speaking_seconds={effective_preferred!r}, "
                        f"max_speaking_seconds={maximum_int!r}. "
                        "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                    )
            except Exception:
                timing_hint = None

            presenter_protocol_message = {
                "role": "system",
                "content": (
                    "PRESENTER MODE PROTOCOL:\n"
                    "- Output EXACTLY TWO tagged blocks and nothing else:\n"
                    "  <spoken>...brief talk track...</spoken>\n"
                    "  <screen>...full on-screen content...</screen>\n"
                    "- <spoken> is what will be read aloud (TTS). Keep it short (1–4 sentences), conversational, and focused on the user's intent and what you did / what to do next. Do not read long lists, code blocks, or raw markdown.\n"
                    "- <screen> is what will be shown. It may include structured markdown, code blocks, and full details.\n"
                    "- Do not include <spoken>/<screen> tags inside code blocks.\n"
                    "- Use New Zealand English spelling."
                    + ("\n\n" + timing_hint if timing_hint else "")
                    + (
                        "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                        "(Applies ONLY to the <spoken> block; do not apply it to <screen>.)\n"
                        + narration_prompt_text
                        if narration_prompt_text
                        else ""
                    )
                    + (
                        "\n\nVON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                        "(Applies ONLY to the <screen> block; it must not override tool-grounded facts.)\n"
                        + screen_prompt_text
                        if screen_prompt_text
                        else ""
                    )
                ),
            }
            enhanced_context.insert(0, presenter_protocol_message)

        # Log the enhanced context being sent to the model for debugging
        current_app.logger.info(
            f"Enhanced context being sent to model: {len(enhanced_context)} messages"
        )
        for i, msg in enumerate(enhanced_context):
            current_app.logger.info(
                "Message %s: role=%s, content_preview=%s...",
                i,
                msg.get("role"),
                _safe_log_preview(msg.get("content", ""), limit=100),
            )

        orchestrator = current_app.config.get("INTERNAL_MCP_ORCHESTRATOR")
        gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
        tool_messages: list[dict[str, str]] = []

        if isinstance(user_namespace, str) and user_namespace.strip():
            current_app.logger.info(
                "[NAMESPACE] Resolved generate namespace=%s source=%s user_concept_id=%s organisation_concept_id=%s",
                user_namespace,
                namespace_source,
                user_concept_id,
                org_concept_id,
            )
            if namespace_report.get("mismatch_detected"):
                current_app.logger.warning(
                    "[NAMESPACE] Mismatch detected during resolution candidates=%s",
                    namespace_report.get("candidates"),
                )
        elif not user_concept_id:
            current_app.logger.warning(
                "[NAMESPACE] No user_concept_id - user_namespace=None (RAG unavailable)"
            )
        else:
            current_app.logger.warning(
                "[NAMESPACE] No effective namespace resolved for user_concept_id=%s",
                user_concept_id,
            )

        rag_trace: dict[str, object] = {
            "authenticated": bool(user_concept_id),
            "namespace": user_namespace,
            "namespace_source": namespace_source,
            "user_concept_id": user_concept_id,
            "organisation_concept_id": org_concept_id,
            "retrieval_attempted": False,
            "retrieval_attempt_reason": (
                None if user_concept_id else "not_authenticated"
            ),
            "tools_invoked": [],
            "tool_results_included_in_prompt": False,
        }
        auxiliary_llm_calls: list[dict[str, Any]] = []
        workflow_continuation_context: dict[str, Any] | None = None
        workflow_discovery_query = prompt_text
        try:
            from ...services.workflow_continuation_service import (
                assess_prompt_for_workflow_continuation,
                build_workflow_continuation_routing_prompt,
                get_session_workflow_continuation_context,
            )

            workflow_continuation_context = get_session_workflow_continuation_context(
                session_id=session_id,
                namespace=user_namespace,
                user_id=history_user_id,
            )
            if isinstance(workflow_continuation_context, Mapping):
                apply_decision = assess_prompt_for_workflow_continuation(
                    prompt=prompt_text,
                    continuation_context=workflow_continuation_context,
                )
                apply_reason = str(apply_decision.get("reason") or "").strip() or None
                apply_signals = [
                    str(item).strip()
                    for item in (apply_decision.get("matched_signals") or [])
                    if str(item).strip()
                ]
                try:
                    continuation_decision_source = (
                        str(apply_decision.get("decision_source") or "").strip()
                        or "workflow_state_check"
                    )
                    auxiliary_llm_calls.append(
                        annotate_python_decision_event(
                            {
                                "type": "workflow_continuation_decision",
                                "stage": "workflow_dispatch",
                                "applies": bool(apply_decision.get("applies", False)),
                                "reason": apply_reason,
                                "signals": list(apply_signals),
                                "session_id": workflow_continuation_context.get(
                                    "session_id"
                                ),
                                "selected_workflow_id": workflow_continuation_context.get(
                                    "selected_workflow_id"
                                ),
                            },
                            stage="workflow_dispatch",
                            component="workflow_continuation_service",
                            function="assess_prompt_for_workflow_continuation",
                            decision_class="continuation_classifier",
                            decision_source=continuation_decision_source,
                            changed_outcome=bool(apply_decision.get("applies", False)),
                            reason_code=apply_reason,
                        )
                    )
                except Exception:
                    pass
                workflow_continuation_context = dict(workflow_continuation_context)
                workflow_continuation_context["applied"] = bool(
                    apply_decision.get("applies", False)
                )
                workflow_continuation_context["apply_reason"] = (
                    str(apply_decision.get("reason") or "").strip() or None
                )
                workflow_continuation_context["apply_signals"] = list(apply_signals)
                if bool(workflow_continuation_context.get("applied")):
                    workflow_discovery_query = (
                        build_workflow_continuation_routing_prompt(
                            prompt=prompt_text,
                            continuation_context=workflow_continuation_context,
                        )
                    )
        except Exception as exc:
            current_app.logger.debug(
                "[WORKFLOW_CONTINUATION] Context lookup skipped: %s",
                exc,
            )
            workflow_continuation_context = None
            workflow_discovery_query = prompt_text

        progress_goal_label = _build_progress_goal_label(
            prompt_text=prompt_text,
            continuation_context=workflow_continuation_context,
        )
        if show_tool_use_progress and progress_goal_label:
            _emit_generate_progress(
                {
                    "status": "thinking",
                    "phase": "context_build",
                    "phase_label": "Building context",
                    "goal_label": progress_goal_label,
                    "request_id": request_id,
                },
            )

        # ---------------------------------------------------------
        # Tool-backed RAG counts (avoid KA vs chat-history confusion)
        # ---------------------------------------------------------
        # We bypass the LLM for simple factual questions about indexed sessions,
        # because the assistant can otherwise confuse:
        # - interaction sessions (KA sessions) vs
        # - chat history sessions.
        def _is_rag_counts_question(text: str) -> bool:
            lowered = (text or "").strip().lower()
            if not lowered:
                return False

            # Keep this intentionally narrow to avoid hijacking normal chat.
            count_triggers = (
                "how many",
                "count",
                "number of",
            )
            domain_triggers = (
                "indexed",
                "rag",
            )
            chat_triggers = (
                "chat session",
                "chat sessions",
                "chat history",
            )
            ka_triggers = (
                "ka",
                "interaction session",
                "interaction sessions",
            )

            has_count = any(t in lowered for t in count_triggers)
            has_domain = any(t in lowered for t in domain_triggers)
            refers_chat = any(t in lowered for t in chat_triggers)
            refers_ka = any(t in lowered for t in ka_triggers)

            # Examples:
            # - "How many indexed chat sessions can you see?"
            # - "How many chat history sessions are indexed?"
            # - "How many KA sessions are indexed?"
            return has_count and has_domain and (refers_chat or refers_ka)

        if (
            user_concept_id
            and user_namespace
            and gateway is not None
            and getattr(gateway, "enabled", False)
            and _is_rag_counts_question(prompt_text)
        ):
            import json as _json

            user_id_for_history: str | None = history_user_id or user_concept_id

            tool_invocations = []
            try:
                tool_result = gateway.invoke(
                    "rag_get_status",
                    {"namespace": user_namespace, "detail": 1},
                )
                payload = tool_result.payload
                duration_ms = getattr(tool_result, "duration_ms", None)

                tool_payload = _json.dumps(
                    {
                        "tool": "rag_get_status",
                        "status": "ok",
                        "duration_ms": duration_ms,
                        "payload": payload,
                    },
                    default=str,
                )
                tool_messages = [{"role": "tool", "content": tool_payload}]
                tool_invocations = [
                    {
                        "tool": "rag_get_status",
                        "payload": {"namespace": user_namespace, "detail": 1},
                        "duration_ms": duration_ms,
                        "direct_user_call": False,
                    }
                ]

                # Summarise counts in a way that makes the KA vs chat distinction explicit.
                rs = payload if isinstance(payload, dict) else {}
                ka_indexed = rs.get("indexed")
                ch_sessions = rs.get("chat_history_sessions_in_namespace")
                if ch_sessions is None:
                    ch_sessions = rs.get("chat_history_sessions")

                ch_details = rs.get("chat_history_session_details")
                fully_indexed = None
                any_indexed = None
                if isinstance(ch_details, list) and ch_details:

                    def _as_int(value):
                        try:
                            return int(value)
                        except Exception:
                            return 0

                    fully_indexed = 0
                    any_indexed = 0
                    for row in ch_details:
                        if not isinstance(row, dict):
                            continue
                        missing = _as_int(row.get("messages_missing_index"))
                        ok = _as_int(row.get("rag_indexed_success"))
                        fail = _as_int(row.get("rag_indexed_failed"))
                        indexed_total = row.get("messages_indexed_total")
                        indexed_total_int = (
                            _as_int(indexed_total)
                            if indexed_total is not None
                            else (ok + fail)
                        )

                        if indexed_total_int > 0:
                            any_indexed += 1
                        if missing <= 0:
                            fully_indexed += 1

                parts = []
                parts.append(
                    f"Chat history sessions (in namespace): {ch_sessions if ch_sessions is not None else '—'}"
                )
                if any_indexed is not None:
                    parts.append(
                        f"Chat history sessions with any indexed messages: {any_indexed}"
                    )
                if fully_indexed is not None:
                    parts.append(
                        f"Chat history sessions fully indexed (missing=0): {fully_indexed}"
                    )
                parts.append(
                    f"KA interaction sessions indexed: {ka_indexed if ka_indexed is not None else '—'}"
                )

                response_text = (
                    "Here are the server-truth counts (RAG status), keeping chat history separate from KA interaction sessions:\n\n"
                    + "\n".join(f"- {p}" for p in parts)
                )
            except Exception as exc:
                response_text = (
                    f"I could not retrieve RAG status via internal tools: {exc}"
                )
                tool_messages = []

            # Persist messages in history/context.
            # NOTE: User message already persisted early (JVNAUTOSCI-1423).
            # Fallback: persist here if early persist failed.
            if not _user_message_persisted_early and user_id_for_history:
                _add_chat_history_message(
                    user_id=user_id_for_history,
                    session_id=session_id,
                    message={
                        "role": "user",
                        "content": prompt_text,
                        "author_user_id": user_concept_id,
                    },
                    namespace=user_namespace,
                    organisation_concept_id=org_concept_id,
                    role_in_org=role_in_org,
                    skip_rag_indexing=True,
                )
            for tool_msg in _truncate_large_tool_results(
                tool_messages, max_tool_content_chars=5000
            ):
                if user_id_for_history:
                    _add_chat_history_message(
                        user_id=user_id_for_history,
                        session_id=session_id,
                        message=tool_msg,
                        namespace=user_namespace,
                        organisation_concept_id=org_concept_id,
                        role_in_org=role_in_org,
                        skip_rag_indexing=True,
                    )
            current_app.config["CONTEXT"] = _limit_context_size(
                current_app.config["CONTEXT"], max_messages=20
            )

            context_stats = _calculate_context_stats(context)
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )
            tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None
            tool_progress_snapshot = _snapshot_tool_progress_for_request(
                progress_scope_key, request_id
            )
            turn_execution_diagnostics = _build_turn_execution_diagnostics(
                request_id=request_id,
                prompt_text=prompt_text,
                elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
                tool_progress_state=tool_progress_snapshot,
            )

            llm_debug_info = {
                "interaction_timestamp_utc": interaction_timestamp_utc,
                "request_id": request_id,
                "model": model_name,
                "llm_interaction": {
                    "requested_model": model_name,
                    "orchestrator_used": False,
                    "duration_ms": None,
                    "usage": None,
                    "calls": [],
                    "server_elapsed_ms": (time.perf_counter() - request_start_perf)
                    * 1000.0,
                },
                "messages": (
                    [{"role": "user", "content": prompt_text}] + tool_messages
                ),
                "response": response_text,
                "user_prompt": user_prompt_debug,
                "context_stats": {
                    "sent_to_llm": context_stats,
                    "stored_context": current_context_stats,
                },
                "tool_stats": tool_stats,
                "tool_invocations": tool_invocations,
                "fastpath": {
                    "name": "rag_counts",
                    "bypassed_llm": True,
                    "used_tool": bool(tool_messages),
                    "enabled": True,
                },
                "turn_execution_diagnostics": turn_execution_diagnostics,
            }
            llm_debug_info = _finalise_llm_debug_info(
                llm_debug_info=llm_debug_info,
                prompt_text=prompt_text,
                response_text=response_text,
                session_id=session_id,
                namespace=user_namespace,
                user_id=user_id_for_history or user_concept_id,
                org_id=org_concept_id,
            )

            if user_id_for_history:
                _add_chat_history_message(
                    user_id=user_id_for_history,
                    session_id=session_id,
                    message={"role": "assistant", "content": response_text},
                    llm_debug_data=llm_debug_info,
                    namespace=user_namespace,
                    organisation_concept_id=org_concept_id,
                    role_in_org=role_in_org,
                    skip_rag_indexing=True,
                )

            return jsonify(
                {
                    "response": response_text,
                    "fastpath": {
                        "name": "rag_counts",
                        "bypassed_llm": True,
                        "used_tool": bool(tool_messages),
                    },
                    "llm_debug": llm_debug_info,
                }
            )

        # ---------------------------------------------------------
        # Optional debug mode: allow user-issued tool calls
        # ---------------------------------------------------------
        # This is disabled by default because it bypasses the LLM's behavioural
        # guardrails. When enabled, only read-category tools are permitted.
        allow_user_tool_calls = os.getenv(
            "VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0"
        ).lower() in {"1", "true"}
        if allow_user_tool_calls and orchestrator is not None and gateway is not None:
            try:
                direct_request = orchestrator._extract_json_blob(prompt_text)  # type: ignore[attr-defined]
            except ToolCallParsingError:
                direct_request = None

            if direct_request and isinstance(direct_request, dict):
                action = direct_request.get("action")
                tool_name = direct_request.get("tool")
                payload = direct_request.get("payload") or {}

                if (
                    action == "call_tool"
                    and isinstance(tool_name, str)
                    and isinstance(payload, dict)
                ):
                    try:
                        meta = gateway.describe_methods().get(tool_name)  # type: ignore[union-attr]
                        category = (
                            meta.get("category") if isinstance(meta, dict) else None
                        )
                    except Exception:
                        category = None

                    if category != "read":
                        response_text = (
                            "Direct tool calls are restricted to read-only tools. "
                            "Ask Von normally if you need write actions."
                        )
                        tool_invocations = []
                    else:
                        # Direct tool-call mode should not mutate payloads except for
                        # Gmail profile convenience (namespace injection can break
                        # strict schemas like Jira tools).
                        if tool_name.startswith("gmail_"):
                            if request_gmail_profile and not payload.get("profile"):
                                payload["profile"] = request_gmail_profile
                            payload.pop("namespace", None)

                        try:
                            result = gateway.invoke(tool_name, payload)  # type: ignore[union-attr]
                            tool_payload = orchestrator._format_tool_result(
                                tool_name, result.payload, result.duration_ms, "ok"
                            )  # type: ignore[attr-defined]
                            response_text = tool_payload
                            tool_messages = [{"role": "tool", "content": tool_payload}]
                            tool_invocations = [
                                {
                                    "tool": tool_name,
                                    "payload": dict(payload),
                                    "direct_user_call": True,
                                }
                            ]
                        except Exception as exc:
                            tool_payload = orchestrator._format_tool_result(
                                tool_name, None, None, "error", str(exc)
                            )  # type: ignore[attr-defined]
                            response_text = tool_payload
                            tool_messages = [{"role": "tool", "content": tool_payload}]
                            tool_invocations = [
                                {
                                    "tool": tool_name,
                                    "payload": dict(payload),
                                    "error": str(exc),
                                    "direct_user_call": True,
                                }
                            ]

                    # Skip LLM generation for direct tool calls
                    # NOTE: User message already persisted early (JVNAUTOSCI-1423).
                    if history_user_id:
                        # Fallback: persist here if early persist failed.
                        if not _user_message_persisted_early:
                            _add_chat_history_message(
                                user_id=history_user_id,
                                session_id=session_id,
                                message={
                                    "role": "user",
                                    "content": prompt_text,
                                    "author_user_id": user_concept_id,
                                },
                                namespace=user_namespace,
                                organisation_concept_id=org_concept_id,
                                role_in_org=role_in_org,
                                skip_rag_indexing=True,
                            )
                        for tool_msg in _truncate_large_tool_results(
                            tool_messages, max_tool_content_chars=5000
                        ):
                            _add_chat_history_message(
                                user_id=history_user_id,
                                session_id=session_id,
                                message=tool_msg,
                                namespace=user_namespace,
                                organisation_concept_id=org_concept_id,
                                role_in_org=role_in_org,
                                skip_rag_indexing=True,
                            )
                    else:
                        current_app.config["CONTEXT"].append(
                            {"role": "user", "content": prompt_text}
                        )
                        for tool_msg in _truncate_large_tool_results(
                            tool_messages, max_tool_content_chars=5000
                        ):
                            current_app.config["CONTEXT"].append(tool_msg)
                        current_app.config["CONTEXT"].append(
                            {"role": "assistant", "content": response_text}
                        )

                    current_app.config["CONTEXT"] = _limit_context_size(
                        current_app.config["CONTEXT"], max_messages=20
                    )

                    # Return immediately with debug info
                    current_turn_messages = [
                        {"role": "user", "content": prompt_text}
                    ] + tool_messages
                    context_stats = _calculate_context_stats(enhanced_context)
                    current_context_stats = _calculate_context_stats(
                        current_app.config["CONTEXT"]
                    )
                    tool_stats = (
                        _calculate_tool_stats(tool_messages) if tool_messages else None
                    )
                    tool_progress_snapshot = _snapshot_tool_progress_for_request(
                        progress_scope_key, request_id
                    )
                    turn_execution_diagnostics = _build_turn_execution_diagnostics(
                        request_id=request_id,
                        prompt_text=prompt_text,
                        elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
                        tool_progress_state=tool_progress_snapshot,
                    )

                    llm_debug_info = {
                        "interaction_timestamp_utc": interaction_timestamp_utc,
                        "request_id": request_id,
                        "model": model_name,
                        "llm_interaction": {
                            "requested_model": model_name,
                            "orchestrator_used": False,
                            "duration_ms": None,
                            "usage": None,
                            "calls": [],
                            "server_elapsed_ms": (
                                time.perf_counter() - request_start_perf
                            )
                            * 1000.0,
                        },
                        "messages": current_turn_messages,
                        "response": response_text,
                        "user_prompt": user_prompt_debug,
                        "namespace_report": namespace_report,
                        "context_stats": {
                            "sent_to_llm": context_stats,
                            "stored_context": current_context_stats,
                        },
                        "tool_stats": tool_stats,
                        "tool_invocations": tool_invocations,
                        "aux_llm_calls": [],
                        "response_transformations": (
                            build_response_transformation_telemetry_payload(
                                request_id=request_id
                            )
                        ),
                        "turn_execution_diagnostics": turn_execution_diagnostics,
                    }
                    llm_debug_info = _finalise_llm_debug_info(
                        llm_debug_info=llm_debug_info,
                        prompt_text=prompt_text,
                        response_text=response_text,
                        session_id=session_id,
                        namespace=user_namespace,
                        user_id=history_user_id or user_concept_id,
                        org_id=org_concept_id,
                    )

                    if history_user_id:
                        _add_chat_history_message(
                            user_id=history_user_id,
                            session_id=session_id,
                            message={"role": "assistant", "content": response_text},
                            llm_debug_data=llm_debug_info,
                            namespace=user_namespace,
                            organisation_concept_id=org_concept_id,
                            role_in_org=role_in_org,
                            skip_rag_indexing=True,
                        )

                    rag_trace["tools_invoked"] = [
                        inv.get("tool")
                        for inv in tool_invocations
                        if isinstance(inv, dict) and isinstance(inv.get("tool"), str)
                    ]
                    rag_trace["retrieval_attempted"] = False
                    rag_trace["retrieval_attempt_reason"] = "direct_tool_call"

                    return jsonify(
                        {
                            "response": response_text,
                            "llm_debug": llm_debug_info,
                            "rag_trace": rag_trace,
                        }
                    )

        llm_interaction: dict = {
            "requested_model": model_name,
            "orchestrator_used": orchestrator is not None,
            "duration_ms": None,
            "usage": None,
            "calls": [],
        }
        render_plan_debug: dict[str, Any] | None = None
        workflow_routing_info: dict[str, Any] | None = None

        def _infer_provider(model_id: str | None) -> str | None:
            if not isinstance(model_id, str):
                return None
            lowered = model_id.strip().lower()
            if not lowered:
                return None
            if lowered.startswith("openai:"):
                return "openai"
            if lowered.startswith(
                ("gpt-", "o1-", "text-", "davinci", "curie", "babbage", "ada")
            ):
                return "openai"
            if lowered.startswith("gemini"):
                return "gemini"
            if lowered.startswith("ollama:"):
                return "ollama"
            if ":" in lowered and not lowered.startswith("ft:"):
                return "ollama"
            return None

        def _record_stage_llm_call(
            *,
            call_type: str,
            model_name: str | None,
            duration_ms: float | None,
            usage: dict | None = None,
            note: str | None = None,
            stage: str | None = None,
            provider: str | None = None,
            candidate: Mapping[str, Any] | None = None,
            workflow_stage_id: str | None = None,
            exchange_blob_ref: Mapping[str, Any] | None = None,
            prompt: Any = None,
            response: Any = None,
            status: str | None = None,
            success: bool | None = None,
            error: str | None = None,
            error_class: str | None = None,
            failure_kind: str | None = None,
        ) -> None:
            payload = {
                "type": call_type,
                "model": model_name,
                "provider": provider or _infer_provider(model_name),
                "duration_ms": duration_ms,
                "usage": usage,
                "workflow": "von_generate",
            }
            if stage:
                payload["stage"] = stage
            if note:
                payload["note"] = note
            if isinstance(candidate, Mapping):
                payload["candidate"] = dict(candidate)
            if isinstance(workflow_stage_id, str) and workflow_stage_id.strip():
                payload["workflow_stage_id"] = workflow_stage_id.strip()
            if isinstance(exchange_blob_ref, Mapping) and exchange_blob_ref:
                payload["exchange_blob_ref"] = dict(exchange_blob_ref)
            if prompt is not None:
                payload["prompt"] = prompt
            if response is not None:
                payload["response"] = response
            if isinstance(status, str) and status.strip():
                payload["status"] = status.strip()
            if isinstance(success, bool):
                payload["success"] = success
            if isinstance(error, str) and error.strip():
                payload["error"] = error.strip()
            if isinstance(error_class, str) and error_class.strip():
                payload["error_class"] = error_class.strip()
            if isinstance(failure_kind, str) and failure_kind.strip():
                payload["failure_kind"] = failure_kind.strip()
            _stamp_llm_call_timestamps(payload, duration_ms=duration_ms)
            llm_interaction["calls"].append(payload)

        def _emit_stage_progress(info: Mapping[str, Any] | None) -> None:
            if not show_tool_use_progress:
                return
            payload = dict(info) if isinstance(info, Mapping) else {"status": "unknown"}
            payload.setdefault("request_id", request_id)
            _emit_generate_progress(payload)

        tool_invocations: list[dict[str, Any]] = []
        conversation_turn_instance_state = _GenerateConversationTurnInstanceState()

        orchestrator_result = None
        orchestrator_input_context = enhanced_context
        if orchestrator is None:
            response_text = _handle_orchestrator_missing_fallback(
                llm_client=llm_client,
                model_name=model_name,
                prompt_text=prompt_text,
                enhanced_context=enhanced_context,
                request_id=request_id,
                gateway=gateway,
                auxiliary_llm_calls=auxiliary_llm_calls,
                llm_interaction=llm_interaction,
            )
        else:
            try:
                current_app.logger.info(
                    "[NAMESPACE] Calling orchestrator.run() with user_namespace=%s",
                    user_namespace,
                )
                try:
                    orchestrator.configure_execution_caps(
                        max_tool_invocations=get_internal_mcp_max_tool_invocations(),
                        tool_batch_cap=get_internal_mcp_tool_batch_cap(),
                    )
                except Exception:
                    # Defensive: never fail the request due to settings refresh.
                    pass

                # JVNAUTOSCI-1038: Create request-scoped progress tracker
                progress_tracker = None
                if progress_updates_enabled:

                    def _progress_update(info: dict[str, Any]) -> None:
                        payload = (
                            dict(info)
                            if isinstance(info, dict)
                            else {"status": "unknown"}
                        )
                        payload.setdefault("request_id", request_id)
                        _mark_background_generate_completed_from_orchestrator_ready_progress(
                            background_task_id=background_task_id or request_id,
                            progress_payload=payload,
                            request_id=request_id,
                            session_id=session_id,
                            created_conversation_session_name=created_conversation_session_name,
                            created_conversation_session=created_conversation_session,
                            user_id=history_user_id or user_concept_id,
                            rag_trace=rag_trace,
                        )
                        _emit_generate_progress(payload)

                    progress_tracker = ProgressTracker(
                        callback=_progress_update,
                        cancellation_checker=(
                            _is_background_cancellation_requested
                            if background_task_id is not None
                            else None
                        ),
                        task_id=background_task_id,
                    )

                if progress_updates_enabled:
                    _emit_generate_progress(
                        {
                            "status": "orchestrator_start",
                            "stage": "workflow_dispatch_prepare",
                            "phase": "workflow_dispatch_prepare",
                            "phase_label": "Preparing workflow dispatch",
                            "subtask": "Prepare routing policy and context",
                            "result_summary": "Preparing workflow dispatch",
                            "goal_label": progress_goal_label,
                            "request_id": request_id,
                        },
                    )

                if progress_updates_enabled:
                    _emit_generate_progress(
                        {
                            "status": "thinking",
                            "stage": "workflow_dispatch_prepare",
                            "phase": "workflow_dispatch_prepare",
                            "phase_label": "Preparing workflow dispatch",
                            "subtask": "conversation turn instance telemetry",
                            "result_summary": "Recording conversation-turn workflow telemetry before supervised execution.",
                            "goal_label": progress_goal_label,
                            "request_id": request_id,
                        },
                    )
                _check_background_cancellation("conversation turn instance telemetry")
                _submit_generate_conversation_turn_instance(
                    state=conversation_turn_instance_state,
                    auxiliary_llm_calls=auxiliary_llm_calls,
                    session_id=session_id,
                    request_id=request_id,
                    user_namespace=user_namespace,
                    user_concept_id=user_concept_id,
                    org_concept_id=org_concept_id,
                    namespace_source=namespace_source,
                    presenter_mode_requested=presenter_mode_requested,
                    request_gmail_profile=request_gmail_profile,
                    request_language=request_language,
                    requested_model=model_name,
                    requested_client_type=explicit_client_type,
                    prompt_text=prompt_text,
                    workflow_discovery_result=workflow_discovery_result,
                    workflow_continuation_context=workflow_continuation_context,
                    get_instance_manager_fn=get_instance_manager,
                    submit_verified_workflow_instance_fn=submit_verified_workflow_instance,
                    logger=current_app.logger,
                )
                if progress_updates_enabled:
                    _emit_generate_progress(
                        {
                            "status": "thinking",
                            "stage": "workflow_dispatch_prepare",
                            "phase": "workflow_dispatch_prepare",
                            "phase_label": "Preparing workflow dispatch",
                            "subtask": "supervised orchestrator entry",
                            "result_summary": "Entering supervised conversation-turn execution.",
                            "goal_label": progress_goal_label,
                            "request_id": request_id,
                        },
                    )
                _check_background_cancellation("supervised orchestrator entry")

                # JVNAUTOSCI-1768: Consolidated entry point. Discovery now happens inside the workflow.
                orchestrator_start_perf = time.perf_counter()
                orchestrator_input_context = list(context or [])
                orchestrator_result = orchestrator.execute_conversation_turn_supervised(
                    prompt=prompt_text,
                    context=orchestrator_input_context,
                    llm_client=llm_client,
                    model=model_name,
                    user_namespace=user_namespace,
                    gmail_profile=request_gmail_profile,
                    auxiliary_system_prompt=dynamic_instructions,
                    preferred_language=request_language,
                    progress_tracker=progress_tracker,
                    conversation_session_id=session_id,
                    turn_id=request_id,
                    workflow_continuation_context=workflow_continuation_context,
                    user_concept_id=user_concept_id,
                    org_concept_id=org_concept_id,
                    turn_memory_context=request_turn_memory_context,
                    turn_expected_outcome_contract=request_turn_expected_outcome_contract,
                    thinking_card_mode=thinking_card_mode,
                )
                llm_interaction["duration_ms"] = (
                    time.perf_counter() - orchestrator_start_perf
                ) * 1000.0
                llm_interaction["calls"] = list(
                    getattr(orchestrator_result, "llm_calls", [])
                )
                llm_interaction["usage"] = getattr(
                    orchestrator_result, "llm_usage", None
                )
                llm_interaction["orchestrator_duration_ms"] = getattr(
                    orchestrator_result, "orchestrator_duration_ms", None
                )
                response_text = orchestrator_result.response_text
                tool_messages = [
                    dict(msg) for msg in orchestrator_result.extra_messages
                ]
                tool_invocations = list(orchestrator_result.tool_invocations)
                if not tool_messages and tool_invocations:
                    tool_messages = _reconstruct_tool_messages_from_invocations(
                        tool_invocations,
                        orchestrator=orchestrator,
                    )
                orchestrator_auxiliary_llm_calls = list(
                    getattr(orchestrator_result, "aux_llm_calls", [])
                )
                if orchestrator_auxiliary_llm_calls:
                    auxiliary_llm_calls.extend(orchestrator_auxiliary_llm_calls)
                raw_render_plan = getattr(orchestrator_result, "render_plan", None)
                if isinstance(raw_render_plan, dict):
                    render_plan_debug = dict(raw_render_plan)
                workflow_discovery_raw = getattr(
                    orchestrator_result, "workflow_discovery_result", None
                )
                if not isinstance(workflow_discovery_raw, Mapping):
                    selected_workflow_trace_raw = getattr(
                        orchestrator_result, "selected_workflow_trace", None
                    )
                    if isinstance(selected_workflow_trace_raw, Mapping):
                        trace_workflow_discovery = selected_workflow_trace_raw.get(
                            "workflow_discovery_result"
                        )
                        if isinstance(trace_workflow_discovery, Mapping):
                            workflow_discovery_raw = trace_workflow_discovery
                workflow_discovery_result = (
                    dict(workflow_discovery_raw)
                    if isinstance(workflow_discovery_raw, Mapping)
                    else None
                )
                if workflow_discovery_result is None:
                    # Self-describing telemetry: if discovery never reached the
                    # route, stamp a sentinel origin so diagnostics surface the
                    # missing payload instead of all-empty defaults that look
                    # indistinguishable from a real empty discovery.
                    workflow_discovery_result = {
                        "discovery_payload_origin": (
                            "missing_from_orchestrator_result"
                        ),
                    }
                workflow_routing_raw = getattr(
                    orchestrator_result, "workflow_routing", None
                )
                workflow_routing_info = _normalise_workflow_routing_payload(
                    workflow_routing_raw
                )

                invoked_tools = []
                for inv in tool_invocations:
                    if not isinstance(inv, dict):
                        continue
                    name = inv.get("tool") or inv.get("method")
                    if isinstance(name, str) and name:
                        invoked_tools.append(name)

                rag_trace["tools_invoked"] = invoked_tools
                rag_trace["retrieval_attempted"] = (
                    "search_knowledge_base" in invoked_tools
                )
                if rag_trace["retrieval_attempted"]:
                    rag_trace["retrieval_attempt_reason"] = "tool_invoked"
                else:
                    rag_trace["retrieval_attempt_reason"] = (
                        "no_rag_retrieval_tool_invoked"
                        if user_concept_id
                        else "not_authenticated"
                    )
                rag_trace["tool_results_included_in_prompt"] = bool(tool_messages)

                if show_tool_use_progress:
                    _emit_generate_progress(
                        {
                            "status": "orchestrator_end",
                            "stage": "orchestrator_end",
                            "phase_label": "Finishing orchestrator",
                            "request_id": request_id,
                        },
                    )
            except ToolCallParsingError as exc:
                current_app.logger.warning(
                    "[mcp_orchestrator] Invalid tool request payload: %s", exc
                )
                response_text = (
                    "Tool call was not executed due to an MCP serialisation error. "
                    f"({exc})\n\n"
                    "Please try again. If this keeps happening, copy the LLM debug output so we can reproduce it."
                )
                rejected_tool_call = _truncate_debug_payload(
                    getattr(exc, "raw_response", None)
                )
                tool_messages = []
                tool_invocations = [
                    {
                        "tool": "__tool_call_parse_error__",
                        "payload": (
                            {"raw_tool_call": rejected_tool_call}
                            if rejected_tool_call is not None
                            else {}
                        ),
                        "error": str(exc),
                    }
                ]

                if show_tool_use_progress:
                    _emit_generate_progress(
                        {
                            "status": "error",
                            "phase": "error",
                            "phase_label": "Error",
                            "request_id": request_id,
                            "error": str(exc),
                        },
                    )

        completion_gate_summary = _latest_turn_completion_gate(auxiliary_llm_calls)
        if isinstance(response_text, str) and isinstance(
            completion_gate_summary, Mapping
        ):
            gate_requires_follow_up = bool(
                completion_gate_summary.get("requires_follow_up", False)
            )
            gate_safe_to_claim_completion = bool(
                completion_gate_summary.get(
                    "safe_to_claim_completion", not gate_requires_follow_up
                )
            )
            if gate_safe_to_claim_completion and not gate_requires_follow_up:
                response_text = strip_completion_ledger_suffix(response_text)

        presenter_channels = _extract_presenter_channels(response_text)

        # Publish a compact background result as soon as the supervised
        # workflow has produced the response. The rest of this route still
        # performs presenter backfill, diagnostics, history persistence, and
        # durable-finalisation best effort, and can enrich the task result
        # later without blocking polling clients on that bookkeeping.
        current_turn_messages = [{"role": "user", "content": prompt_text}]
        if tool_messages:
            current_turn_messages.extend(tool_messages)
        selected_workflow_trace_payload = (
            dict(raw_selected_workflow_trace)
            if isinstance(
                raw_selected_workflow_trace := getattr(
                    orchestrator_result,
                    "selected_workflow_trace",
                    None,
                ),
                Mapping,
            )
            else None
        )
        serialised_tool_invocations = _serialise_tool_invocations_for_llm_debug(
            tool_invocations
        )
        background_ready_llm_debug: dict[str, Any] = {
            "interaction_timestamp_utc": interaction_timestamp_utc,
            "request_id": request_id,
            "model": model_name,
            "llm_interaction": {
                **llm_interaction,
                "server_elapsed_ms": (time.perf_counter() - request_start_perf)
                * 1000.0,
            },
            "messages": current_turn_messages,
            "response": response_text,
            "presenter_channels": presenter_channels,
            "user_prompt": user_prompt_debug,
            "namespace_report": namespace_report,
            "tool_invocations": serialised_tool_invocations,
            "aux_llm_calls": auxiliary_llm_calls,
            "workflow_discovery": workflow_discovery_result,
            "workflow_routing": workflow_routing_info,
            "selected_workflow_trace": selected_workflow_trace_payload,
            "display_elements": None,
            "background_result_source": "generate_response_ready",
        }
        if isinstance(render_plan_debug, dict):
            background_ready_llm_debug["render_plan"] = dict(render_plan_debug)
        background_ready_body = _build_generate_success_body(
            request_id=request_id,
            session_id=session_id,
            created_conversation_session_name=created_conversation_session_name,
            created_conversation_session=created_conversation_session,
            response_text=response_text,
            presenter_channels=(
                presenter_channels if isinstance(presenter_channels, dict) else None
            ),
            llm_debug_info=background_ready_llm_debug,
            display_elements_contract=None,
            rag_trace=rag_trace,
        )
        _mark_background_generate_completed_if_ready(
            background_task_id=background_task_id,
            result_body=background_ready_body,
            request_id=request_id,
            session_id=session_id,
            user_id=history_user_id or user_concept_id,
            response_text=response_text,
        )

        presenter_channels_missing = (
            not isinstance(presenter_channels, dict) or not presenter_channels
        )
        has_tool_messages = bool(tool_messages)
        response_transformations = build_response_transformation_telemetry_payload(
            request_id=request_id
        )

        screen_backfill_started_perf = time.perf_counter()
        screen_backfill_second_pass_attempted = False
        screen_backfill_second_pass_reason = None
        screen_backfill_source = None
        screen_backfill_model_id = None
        screen_backfill_error_class = None
        screen_backfill_applied = False
        screen_backfill_screen_tag_present: bool | None = None
        needs_screen_backfill = False
        required_screen_json_fence = None
        screen_fence_compat_enabled = get_display_elements_screen_fence_compat_enabled(
            default=True
        )

        if presenter_mode_requested:
            screen_tag_present = _presenter_tag_present(response_text, "screen")
            screen_backfill_screen_tag_present = screen_tag_present
            required_screen_json_fence = _extract_required_screen_json_fence(
                prompt_text
            )
            screen_text = None
            spoken_text = None
            if isinstance(presenter_channels, dict):
                screen_value = presenter_channels.get("screen")
                if isinstance(screen_value, str) and screen_value.strip():
                    screen_text = screen_value.strip()
                spoken_value = presenter_channels.get("spoken")
                if isinstance(spoken_value, str) and spoken_value.strip():
                    spoken_text = spoken_value.strip()

            def _screen_looks_like_tool_dump(value: str) -> bool:
                lowered = (value or "").strip().lower()
                if not lowered:
                    return True
                if lowered.startswith("tool results:"):
                    return True
                if "```json" in lowered or '"payload"' in lowered:
                    return True
                return False

            def _screen_too_similar_to_spoken(
                screen_value: str | None, spoken_value: str | None
            ) -> bool:
                if not screen_value or not spoken_value:
                    return False
                a = screen_value.strip()
                b = spoken_value.strip()
                if not a or not b:
                    return False
                # Heuristic: if the screen is exactly the talk track, it usually means the
                # model emitted only <spoken> and we defaulted screen=spoken.
                return a == b

            existing_screen_tool_dump = bool(
                isinstance(screen_text, str)
                and _screen_looks_like_tool_dump(screen_text)
            )
            existing_screen_internal_status = bool(
                isinstance(screen_text, str)
                and _looks_like_internal_status_diagnostic(screen_text)
            )
            if existing_screen_tool_dump:
                _append_presenter_detector_event(
                    auxiliary_llm_calls,
                    function_name="_presenter_screen_backfill_detector",
                    detector="screen_tool_dump",
                    context="existing_screen_candidate",
                    reason_code="existing_screen_candidate_tool_dump_detected",
                    preview=screen_text,
                )
            if existing_screen_internal_status:
                _append_presenter_detector_event(
                    auxiliary_llm_calls,
                    function_name="_presenter_screen_backfill_detector",
                    detector="internal_status_diagnostic",
                    context="existing_screen_candidate",
                    reason_code="existing_screen_candidate_internal_status_detected",
                    preview=screen_text,
                )

            needs_screen_backfill = (
                (not screen_tag_present)
                or (not isinstance(screen_text, str) or not screen_text.strip())
                or existing_screen_tool_dump
                or existing_screen_internal_status
                or (
                    screen_fence_compat_enabled
                    and isinstance(required_screen_json_fence, str)
                    and required_screen_json_fence.strip()
                    and (
                        not isinstance(screen_text, str)
                        or required_screen_json_fence.strip() not in screen_text
                    )
                )
                or _screen_too_similar_to_spoken(screen_text, spoken_text)
            )

            def _missing_required_screen_fence() -> bool:
                return bool(
                    screen_fence_compat_enabled
                    and isinstance(required_screen_json_fence, str)
                    and required_screen_json_fence.strip()
                    and (
                        not isinstance(screen_text, str)
                        or required_screen_json_fence.strip() not in screen_text
                    )
                )

            def _strip_presenter_tags(text: str) -> str | None:
                if not isinstance(text, str) or not text:
                    return None
                import re

                cleaned = re.sub(r"</?spoken>", "", text, flags=re.IGNORECASE)
                cleaned = re.sub(r"</?screen>", "", cleaned, flags=re.IGNORECASE)
                cleaned = cleaned.strip()
                return cleaned or None

            if needs_screen_backfill:
                screen_backfill_second_pass_attempted = True
                if show_tool_use_progress:
                    _emit_generate_progress(
                        {
                            "status": "phase_transition",
                            "phase": "screen_backfill",
                            "phase_label": "Generating response",
                            "request_id": request_id,
                            "workflow_task": "screen_backfill",
                        },
                    )
                if not screen_tag_present or not screen_text:
                    screen_backfill_second_pass_reason = "missing_screen"
                elif _missing_required_screen_fence():
                    screen_backfill_second_pass_reason = "missing_screen_fence"
                elif _screen_looks_like_tool_dump(screen_text or ""):
                    screen_backfill_second_pass_reason = "tool_payload_screen"
                elif _looks_like_internal_status_diagnostic(screen_text or ""):
                    screen_backfill_second_pass_reason = "internal_status_screen"
                else:
                    screen_backfill_second_pass_reason = "screen_matches_spoken"

                tool_blob = (
                    _build_tool_messages_prompt_blob(tool_messages)
                    if has_tool_messages
                    else ""
                )

                def _tool_messages_include_description_write(
                    messages: list[dict],
                ) -> bool:
                    """Best-effort detection of a description write tool action.

                    We keep this conservative: if we cannot identify a description
                    write confidently, return False.
                    """

                    import json

                    for message in messages:
                        if message.get("role") != "tool":
                            continue
                        content = message.get("content")
                        if not isinstance(content, str) or not content.strip():
                            continue

                        try:
                            parsed = json.loads(content)
                        except Exception:
                            continue

                        if not isinstance(parsed, dict):
                            continue

                        tool_name = (
                            parsed.get("tool")
                            or parsed.get("method")
                            or parsed.get("name")
                            or ""
                        )
                        tool_name = tool_name if isinstance(tool_name, str) else ""
                        tool_name_lower = tool_name.lower()

                        payload = parsed.get("payload")
                        payload = payload if isinstance(payload, dict) else {}

                        if "description" in tool_name_lower:
                            return True

                        predicate = payload.get("predicate")
                        if isinstance(predicate, str) and predicate.strip() in {
                            "hasDescription",
                            "has_description",
                            "#V#hasDescription",
                        }:
                            return True

                        if (
                            isinstance(payload.get("description"), str)
                            and payload.get("description").strip()
                        ):
                            return True

                    return False

                description_write_seen = (
                    _tool_messages_include_description_write(tool_messages)
                    if has_tool_messages
                    else False
                )

                def _append_operational_summary(
                    base_text: str,
                    summary_text: str | None,
                ) -> str:
                    cleaned_base = str(base_text or "").strip()
                    cleaned_summary = str(summary_text or "").strip()
                    if not cleaned_summary:
                        return cleaned_base
                    if not cleaned_base:
                        return cleaned_summary
                    if cleaned_summary in cleaned_base:
                        return cleaned_base
                    return f"{cleaned_base}\n\nOperational summary:\n{cleaned_summary}"

                screen_candidate = None
                screen_backfill_source = None
                follow_up_screen_summary = None
                supplementary_screen_summary = None
                tool_summary_projection_telemetry: dict[str, Any] = {}
                if has_tool_messages and isinstance(completion_gate_summary, Mapping):
                    requires_follow_up = bool(
                        completion_gate_summary.get("requires_follow_up", False)
                    )
                    safe_to_claim_completion = bool(
                        completion_gate_summary.get(
                            "safe_to_claim_completion", not requires_follow_up
                        )
                    )
                    if requires_follow_up or not safe_to_claim_completion:
                        follow_up_screen_summary = (
                            _build_presenter_follow_up_summary_from_tool_messages(
                                tool_messages,
                                completion_gate=completion_gate_summary,
                                auxiliary_llm_calls=auxiliary_llm_calls,
                            )
                        )
                if follow_up_screen_summary:
                    supplementary_screen_summary = follow_up_screen_summary
                response_candidate = _strip_presenter_tags(response_text)
                response_candidate_internal_status = (
                    _looks_like_internal_status_diagnostic(response_candidate)
                )
                response_candidate_tool_dump = bool(
                    isinstance(response_candidate, str)
                    and _screen_looks_like_tool_dump(response_candidate)
                )
                response_candidate_duplicates_spoken = _screen_too_similar_to_spoken(
                    response_candidate, spoken_text
                )
                if (
                    response_candidate
                    and not response_candidate_duplicates_spoken
                    and response_candidate_tool_dump
                ):
                    _append_presenter_detector_event(
                        auxiliary_llm_calls,
                        function_name="_presenter_screen_backfill_detector",
                        detector="screen_tool_dump",
                        context="response_candidate_reuse",
                        reason_code="response_candidate_tool_dump_rejected",
                        preview=response_candidate,
                    )
                if (
                    response_candidate
                    and not response_candidate_duplicates_spoken
                    and response_candidate_internal_status
                ):
                    _append_presenter_detector_event(
                        auxiliary_llm_calls,
                        function_name="_presenter_screen_backfill_detector",
                        detector="internal_status_diagnostic",
                        context="response_candidate_reuse",
                        reason_code="response_candidate_internal_status_rejected",
                        preview=response_candidate,
                    )
                # If the model emitted only <spoken>, response_candidate usually equals
                # spoken text. Reusing it would keep screen/spoken identical.
                if (
                    screen_candidate is None
                    and response_candidate
                    and not response_candidate_duplicates_spoken
                    and not response_candidate_tool_dump
                    and not response_candidate_internal_status
                ):
                    screen_candidate = _append_operational_summary(
                        response_candidate,
                        supplementary_screen_summary,
                    )
                    screen_backfill_source = (
                        "response_text_plus_follow_up_summary"
                        if supplementary_screen_summary
                        else "response_text"
                    )
                    if supplementary_screen_summary:
                        auxiliary_llm_calls.append(
                            annotate_python_decision_event(
                                {
                                    "type": "presenter_screen_backfill",
                                    "stage": "screen_backfill",
                                    "source": "response_text_plus_follow_up_summary",
                                },
                                stage="screen_backfill",
                                component="presenter_routes",
                                function="_build_presenter_follow_up_summary_from_tool_messages",
                                decision_class="presenter_fallback",
                                decision_source="structural_pattern_detection",
                                changed_outcome=True,
                                reason_code="response_text_supplemented_with_follow_up_summary",
                                possible_inappropriate_python_code_use=True,
                            )
                        )
                allow_llm_screen_synthesis = os.getenv(
                    "VON_PRESENTER_SCREEN_BACKFILL_USE_LLM", "1"
                ).lower() in {"1", "true"}

                if screen_candidate is None and allow_llm_screen_synthesis:
                    try:
                        if has_tool_messages:
                            synthesis_system = (
                                "You are Von. Create the on-screen response for the chat UI. "
                                "Return ONLY one block: <screen>...</screen>. "
                                "Do not include <spoken>. Do not include JSON. "
                                "Use New Zealand English spelling. "
                                "Use only the supplied user request, represented screen prompt, model response, and tool/evidence summary."
                            )
                            synthesis_user = (
                                "User request:\n"
                                f"{prompt_text}\n\n"
                                "Model response (may be incomplete; NOT authoritative for tool-backed changes):\n"
                                f"{response_text}\n\n"
                                + (
                                    "VON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                                    "(Applies ONLY to <screen> formatting; it must not override tool-grounded facts.)\n"
                                    + str(screen_prompt_text).strip()
                                    + "\n\n"
                                    if isinstance(screen_prompt_text, str)
                                    and screen_prompt_text.strip()
                                    else ""
                                )
                                + "Tool/evidence summary:\n"
                                f"{tool_blob}\n"
                                + (
                                    "- The model response contains internal execution diagnostics. Rewrite them into user-facing screen content. "
                                    "Do not copy raw labels like 'Execution status', 'Blocking effect IDs', 'Unresolved preconditions', "
                                    "or 'Failure codes' unless the user explicitly asked for diagnostics.\n"
                                    if response_candidate_internal_status
                                    else ""
                                )
                            )
                        else:
                            synthesis_system = (
                                "You are Von. Create the on-screen response for the chat UI. "
                                "Return ONLY one block: <screen>...</screen>. "
                                "Do not include <spoken>. Do not include JSON. "
                                "Use New Zealand English spelling. "
                                "Use only the user request and the model response as sources. "
                                "Do not invent facts beyond what is stated there."
                            )
                            synthesis_user = (
                                "User request:\n"
                                f"{prompt_text}\n\n"
                                "Model response (may be incomplete; use it as content to display):\n"
                                f"{response_text}\n\n"
                                + (
                                    "VON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                                    "(Applies ONLY to <screen> formatting.)\n"
                                    + str(screen_prompt_text).strip()
                                    + "\n\n"
                                    if isinstance(screen_prompt_text, str)
                                    and screen_prompt_text.strip()
                                    else ""
                                )
                                + (
                                    "Important:\n"
                                    "- The model response is internal execution-status text, not final user-facing screen copy.\n"
                                    "- Rewrite it into a concise user-facing screen answer that explains what happened.\n"
                                    "- Do not repeat raw labels like 'Execution status', 'Blocking effect IDs', "
                                    "'Unresolved preconditions', or 'Failure codes' unless the user explicitly asked for diagnostics.\n"
                                    if response_candidate_internal_status
                                    else ""
                                )
                            )

                        synthesis_response = None
                        if orchestrator is not None and hasattr(
                            orchestrator, "_run_llm_with_fallbacks"
                        ):
                            try:
                                policy_state, registry_snapshot = (
                                    orchestrator._load_workflow_model_policy(
                                        request_language
                                    )
                                )
                                synthesis_response, screen_model_used, _ = (
                                    orchestrator._run_llm_with_fallbacks(
                                        stage="screen_backfill",
                                        prompt="Generate <screen> display content",
                                        context=[
                                            {
                                                "role": "system",
                                                "content": synthesis_system,
                                            },
                                            {"role": "user", "content": synthesis_user},
                                        ],
                                        default_client=llm_client,
                                        default_model=model_name,
                                        policy_state=policy_state,
                                        registry_snapshot=(
                                            registry_snapshot
                                            if isinstance(registry_snapshot, Mapping)
                                            else None
                                        ),
                                        user_concept_id=user_concept_id,
                                        org_concept_id=org_concept_id,
                                        llm_calls_log=llm_interaction["calls"],
                                        aux_log=auxiliary_llm_calls,
                                        record_llm_call=_record_stage_llm_call,
                                        emit_progress=_emit_stage_progress,
                                    )
                                )
                                screen_backfill_model_id = screen_model_used
                            except Exception:
                                synthesis_response = None
                        if synthesis_response is None:
                            llm_start = time.perf_counter()
                            screen_model_used = model_name
                            screen_backfill_model_id = screen_model_used
                            _emit_stage_progress(
                                {
                                    "status": "llm_call_start",
                                    "stage": "screen_backfill",
                                    "model": screen_model_used,
                                }
                            )
                            synthesis_response = _llm_generate_screen_backfill(
                                llm_client,
                                synthesis_system,
                                synthesis_user,
                                screen_model_used,
                            )
                            screen_duration_ms = (
                                time.perf_counter() - llm_start
                            ) * 1000.0
                            _emit_stage_progress(
                                {
                                    "status": "llm_call_chunk",
                                    "stage": "screen_backfill",
                                    "model": screen_model_used,
                                    "chunks": 1,
                                    "duration_ms": int(screen_duration_ms),
                                }
                            )
                            _emit_stage_progress(
                                {
                                    "status": "llm_call_end",
                                    "stage": "screen_backfill",
                                    "model": screen_model_used,
                                    "duration_ms": int(screen_duration_ms),
                                    "success": True,
                                    "error": None,
                                }
                            )
                            _record_stage_llm_call(
                                call_type="llm.generate",
                                model_name=screen_model_used,
                                duration_ms=screen_duration_ms,
                                usage=None,
                                note="Screen backfill synthesis (legacy).",
                                stage="screen_backfill",
                                provider=_infer_provider(screen_model_used),
                                prompt={
                                    "text": (
                                        "System:\n"
                                        f"{synthesis_system}\n\nUser:\n{synthesis_user}"
                                    ),
                                    "char_count": len(
                                        "System:\n"
                                        f"{synthesis_system}\n\nUser:\n{synthesis_user}"
                                    ),
                                    "is_truncated": False,
                                },
                                response={
                                    "text": str(synthesis_response),
                                    "char_count": len(str(synthesis_response)),
                                    "is_truncated": False,
                                },
                            )

                        screen_candidate = _extract_screen_only(str(synthesis_response))
                        if not screen_candidate:
                            raw = str(synthesis_response).strip()
                            if raw:
                                screen_candidate = raw
                        if screen_candidate:
                            screen_backfill_source = "llm_synthesis"
                            auxiliary_llm_calls.append(
                                annotate_python_decision_event(
                                    {
                                        "type": "presenter_screen_backfill",
                                        "stage": "screen_backfill",
                                        "source": "llm_synthesis",
                                        "diagnostic_rewrite": response_candidate_internal_status,
                                    },
                                    stage="screen_backfill",
                                    component="presenter_routes",
                                    function="_presenter_llm_screen_synthesis",
                                    decision_class="presenter_fallback",
                                    decision_source="llm_synthesis",
                                    changed_outcome=True,
                                    reason_code=(
                                        "diagnostic_ledger_rewritten"
                                        if response_candidate_internal_status
                                        else "screen_synthesised_from_tools"
                                    ),
                                    possible_inappropriate_python_code_use=bool(
                                        response_candidate_internal_status
                                    ),
                                )
                            )
                    except Exception as exc:
                        screen_backfill_error_class = type(exc).__name__
                        screen_candidate = None

                # Legacy support-only safety net: if the screen model claims a
                # description write without tool evidence, discard it and expose
                # the deterministic evidence summary instead.
                if (
                    screen_candidate
                    and screen_backfill_source == "llm_synthesis"
                    and not description_write_seen
                    and isinstance(screen_candidate, str)
                ):
                    import re

                    lowered = screen_candidate.lower()
                    description_claim_patterns = (
                        r"\bdescription\s+(?:has\s+been\s+)?(?:added|updated|set|filled)\b",
                        r"\badded\s+(?:a\s+)?description\b",
                        r"\bupdated\s+(?:the\s+)?description\b",
                    )
                    if any(
                        re.search(p, lowered, flags=re.IGNORECASE)
                        for p in description_claim_patterns
                    ):
                        screen_candidate = None
                        auxiliary_llm_calls.append(
                            annotate_python_decision_event(
                                {
                                    "type": "presenter_screen_backfill",
                                    "stage": "screen_backfill",
                                    "source": "llm_synthesis",
                                    "suppressed_claim": "description_write_without_tool_evidence",
                                },
                                stage="screen_backfill",
                                component="presenter_routes",
                                function="_presenter_screen_backfill_description_claim_guard",
                                decision_class="presenter_support_safety_net",
                                decision_source="structural_pattern_detection",
                                changed_outcome=True,
                                reason_code="description_claim_without_tool_evidence_suppressed",
                                possible_inappropriate_python_code_use=True,
                            )
                        )

                if not screen_candidate:
                    fallback_summary = supplementary_screen_summary or (
                        _build_presenter_screen_summary_from_tool_messages(
                            tool_messages,
                            projection_telemetry=tool_summary_projection_telemetry,
                        )
                    )
                    if fallback_summary:
                        screen_candidate = _append_operational_summary(
                            "",
                            fallback_summary,
                        )
                        screen_backfill_source = (
                            "follow_up_summary"
                            if supplementary_screen_summary
                            else "tool_summary"
                        )
                        reason_code = (
                            "tool_backed_follow_up_summary"
                            if supplementary_screen_summary
                            else "tool_activity_summary_fallback"
                        )
                        auxiliary_llm_calls.append(
                            annotate_python_decision_event(
                                {
                                    "type": "presenter_screen_backfill",
                                    "stage": "screen_backfill",
                                    "source": screen_backfill_source,
                                    "represented_evidence_projection": (
                                        dict(tool_summary_projection_telemetry)
                                        if tool_summary_projection_telemetry
                                        else None
                                    ),
                                },
                                stage="screen_backfill",
                                component="presenter_routes",
                                function=(
                                    "_build_presenter_follow_up_summary_from_tool_messages"
                                    if supplementary_screen_summary
                                    else "_build_presenter_screen_summary_from_tool_messages"
                                ),
                                decision_class="presenter_fallback",
                                decision_source="structural_pattern_detection",
                                changed_outcome=True,
                                reason_code=reason_code,
                                possible_inappropriate_python_code_use=True,
                            )
                        )

                if screen_candidate:
                    if screen_fence_compat_enabled:
                        screen_candidate = _ensure_required_screen_json_fence(
                            str(screen_candidate).strip(),
                            required_screen_json_fence,
                        )
                    base_channels = (
                        dict(presenter_channels)
                        if isinstance(presenter_channels, dict)
                        else {}
                    )
                    base_channels["screen"] = str(screen_candidate).strip()
                    if screen_backfill_source == "response_text":
                        base_channels["format"] = "screen_backfill_from_response_v1"
                    elif (
                        screen_backfill_source == "response_text_plus_follow_up_summary"
                    ):
                        base_channels["format"] = (
                            "screen_backfill_from_response_with_operational_summary_v1"
                        )
                    elif screen_backfill_source == "follow_up_summary":
                        base_channels["format"] = (
                            "screen_backfill_from_follow_up_summary_v1"
                        )
                    else:
                        base_channels["format"] = "screen_backfill_from_tools_v1"
                    presenter_channels = base_channels
                    screen_backfill_applied = True

                    auxiliary_llm_calls.append(
                        {
                            "type": "workflow_stage",
                            "stage": "screen_backfill",
                            "source": screen_backfill_source,
                            "reason": screen_backfill_second_pass_reason,
                            "format": base_channels.get("format"),
                        }
                    )

        screen_backfill_latency_ms = (
            time.perf_counter() - screen_backfill_started_perf
        ) * 1000.0
        if not presenter_mode_requested:
            screen_backfill_status = "skipped"
            screen_backfill_suppression_reason = "presenter_mode_disabled"
        elif not needs_screen_backfill:
            screen_backfill_status = "skipped"
            screen_backfill_suppression_reason = "not_required"
        elif screen_backfill_applied:
            screen_backfill_status = (
                "success"
                if screen_backfill_source == "llm_synthesis"
                else "fallback_success"
            )
            screen_backfill_suppression_reason = None
        elif screen_backfill_error_class:
            screen_backfill_status = "failure"
            screen_backfill_suppression_reason = "model_error"
        else:
            screen_backfill_status = "no_op"
            screen_backfill_suppression_reason = (
                screen_backfill_second_pass_reason or "no_candidates"
            )

        record_response_transformation_event(
            response_transformations,
            event=build_response_transformation_event(
                transform_name="screen_backfill",
                transform_version="v1",
                status=screen_backfill_status,
                input_summary={
                    "presenter_mode_requested": presenter_mode_requested,
                    "screen_tag_present": screen_backfill_screen_tag_present,
                    "needs_backfill": needs_screen_backfill,
                    "tool_message_count": len(tool_messages),
                    "required_screen_json_fence": bool(
                        isinstance(required_screen_json_fence, str)
                        and required_screen_json_fence.strip()
                    ),
                },
                output_summary={
                    "applied": screen_backfill_applied,
                    "presenter_format": (
                        presenter_channels.get("format")
                        if isinstance(presenter_channels, dict)
                        else None
                    ),
                    "reason": screen_backfill_second_pass_reason,
                },
                options_emitted_count=0,
                source_path=screen_backfill_source,
                latency_ms=screen_backfill_latency_ms,
                model_id=screen_backfill_model_id,
                suppression_reason=screen_backfill_suppression_reason,
                error_class=screen_backfill_error_class,
            ),
        )

        def _extract_spoken_only(text: str) -> str | None:
            return _extract_tagged_block(text, "spoken")

        def _coerce_spoken_text(text: object) -> str | None:
            """Best-effort normalisation for narration responses.

            The narration second-pass *should* return <spoken>...</spoken>, but in
            practice models sometimes return plain text. Accept either format.
            """

            if text is None:
                return None

            raw = str(text).strip()
            if not raw:
                return None

            tagged = _extract_spoken_only(raw)
            if tagged:
                return tagged

            import re

            # Strip any accidental tagged blocks and code fences.
            raw = re.sub(r"</?spoken>", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"</?screen>", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
            raw = raw.replace("`", "")
            raw = raw.strip()

            if not raw:
                return None

            # Keep it short for TTS.
            try:
                max_chars = int(os.getenv("VON_NARRATION_SPOKEN_MAX_CHARS", "4000"))
            except ValueError:
                max_chars = 4000
            max_chars = max(200, min(max_chars, 50000))
            if len(raw) > max_chars:
                clipped = raw[:max_chars].rstrip()
                # Prefer clipping at a sentence boundary.
                for sep in (". ", "! ", "? "):
                    cut = clipped.rfind(sep)
                    if cut > 200:
                        clipped = clipped[: cut + 1]
                        break
                raw = clipped.strip()

            return raw or None

        spoken_backfill_started_perf = time.perf_counter()
        spoken_backfill_second_pass_attempted = False
        spoken_backfill_second_pass_reason = None
        spoken_backfill_source = None
        spoken_backfill_model_id = None
        spoken_backfill_error_class = None
        spoken_backfill_applied = False

        # If presenter mode was requested but the model didn't produce a usable
        # <spoken> channel, generate it in a second pass for reliability.
        #
        # Note: The model may emit only <screen>...</screen>. In that case,
        # we still generate <spoken> using the narration prompt rather than
        # falling back to reading the screen/markdown verbatim.
        needs_spoken_backfill = False
        spoken_from_screen_text = False
        if presenter_mode_requested:
            if presenter_channels_missing:
                needs_spoken_backfill = True
                if has_tool_messages:
                    spoken_backfill_second_pass_reason = "missing_spoken"
                else:
                    spoken_backfill_second_pass_reason = "missing_presenter_channels"
            elif isinstance(presenter_channels, dict):
                spoken_value = presenter_channels.get("spoken")
                if not isinstance(spoken_value, str) or not spoken_value.strip():
                    needs_spoken_backfill = True
                    spoken_backfill_second_pass_reason = "missing_spoken"
            else:
                needs_spoken_backfill = True
                spoken_backfill_second_pass_reason = "missing_presenter_channels"

        if needs_spoken_backfill:
            try:
                spoken_backfill_second_pass_attempted = True
                if show_tool_use_progress:
                    _emit_generate_progress(
                        {
                            "status": "phase_transition",
                            "phase": "narration",
                            "phase_label": "Generating narration",
                            "request_id": request_id,
                            "workflow_task": "narration_planning",
                        },
                    )
                if isinstance(presenter_channels, dict) and isinstance(
                    presenter_channels.get("screen"), str
                ):
                    screen_text = str(presenter_channels.get("screen") or "").strip()
                else:
                    screen_text = (
                        response_text.strip()
                        if isinstance(response_text, str)
                        else str(response_text)
                    )

                narration_data = {
                    "presenter_mode_requested": presenter_mode_requested,
                    "screen_text": screen_text,
                    "user_prompt": prompt_text,
                    "narration_prompt_text": narration_prompt_text,
                    "narration_prompt_ids": [
                        frag.get("concept_id")
                        for frag in (narration_prompt_fragments or [])
                        if isinstance(frag, dict)
                    ],
                    "presenter_channels": (
                        dict(presenter_channels)
                        if isinstance(presenter_channels, dict)
                        else {}
                    ),
                }

                narration_trace = None
                narration_trace_store = None
                narration_trace_enabled = os.getenv(
                    "VON_WORKFLOWS_TRACE_ENABLED", "0"
                ).lower() in {"1", "true"}
                if narration_trace_enabled:
                    try:
                        narration_trace = WorkflowExecutionTrace(
                            workflow_id=CHAT_NARRATION_WORKFLOW_ID
                        )
                        narration_trace.user_namespace = user_namespace
                        narration_trace_store = insert_workflow_execution_trace
                    except Exception:
                        narration_trace = None
                        narration_trace_store = None
                        narration_trace_enabled = False

                workflow_result = None
                if orchestrator is not None:
                    workflow_result = orchestrator.execute_workflow(
                        CHAT_NARRATION_WORKFLOW_ID,
                        data=narration_data,
                        llm_client=llm_client,
                        model=model_name,
                        user_namespace=user_namespace,
                        auxiliary_system_prompt=dynamic_instructions,
                        trace=narration_trace,
                    )

                if workflow_result is not None:
                    spoken_backfill_source = "workflow"
                    channels = workflow_result.data.get(
                        "presenter_channels", presenter_channels
                    )
                    if isinstance(channels, dict) and channels:
                        presenter_channels = channels
                        workflow_spoken = channels.get("spoken")
                        if isinstance(workflow_spoken, str) and workflow_spoken.strip():
                            spoken_backfill_applied = True
                    if narration_trace_enabled and narration_trace is not None:
                        try:
                            if workflow_result.completed:
                                narration_trace.finish_completed()
                            elif workflow_result.error:
                                narration_trace.finish_failed(workflow_result.error)
                        except Exception:
                            pass
                        if narration_trace_store is not None:
                            try:
                                stored_exec = narration_trace_store(
                                    narration_trace.to_storage_document()
                                )
                                auxiliary_llm_calls.append(
                                    {
                                        "type": "workflow_execution_trace",
                                        "path": "narration",
                                        "workflow_id": CHAT_NARRATION_WORKFLOW_ID,
                                        "execution_id": narration_trace.execution_id,
                                        "stored": bool(stored_exec),
                                        "status": narration_trace.status,
                                    }
                                )
                            except Exception:
                                pass
                else:
                    # Include a small client-reported timing hint for narration generation.
                    # (Non-authoritative; used only for guidance.)
                    timing_hint = None
                    try:
                        from ...services.client_capabilities_service import (
                            get_client_capabilities_snapshot,
                        )

                        snapshot = get_client_capabilities_snapshot()
                        speech = (
                            snapshot.get("speech_synthesis")
                            if isinstance(snapshot, dict)
                            else None
                        )
                        speech = speech if isinstance(speech, dict) else {}
                        raw_settings = speech.get("settings")
                        settings = (
                            raw_settings if isinstance(raw_settings, dict) else {}
                        )

                        preferred = settings.get("preferred_speaking_seconds")
                        maximum = settings.get("max_speaking_seconds")

                        try:
                            preferred_int = (
                                int(preferred) if preferred is not None else None
                            )
                        except Exception:
                            preferred_int = None

                        try:
                            maximum_int = int(maximum) if maximum is not None else None
                        except Exception:
                            maximum_int = None

                        if preferred_int is not None:
                            preferred_int = max(1, min(preferred_int, 600))
                        if maximum_int is not None:
                            maximum_int = max(1, min(maximum_int, 600))

                        effective_preferred = preferred_int
                        if preferred_int is not None and maximum_int is not None:
                            effective_preferred = min(preferred_int, maximum_int)

                        if effective_preferred is not None or maximum_int is not None:
                            timing_hint = (
                                "Speech timing hint (client-reported, non-authoritative): "
                                f"preferred_speaking_seconds={effective_preferred!r}, "
                                f"max_speaking_seconds={maximum_int!r}. "
                                "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                            )
                    except Exception:
                        timing_hint = None

                    narration_system = (
                        "You are Von. Produce a short talk track for text-to-speech. "
                        "Return ONLY one block: <spoken>...</spoken>. "
                        "Do not include <screen>. Do not include code blocks. "
                        "Use New Zealand English spelling."
                        + ("\n\n" + timing_hint if timing_hint else "")
                        + (
                            "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                            + narration_prompt_text
                            if narration_prompt_text
                            else ""
                        )
                    )

                    narration_user = (
                        "User message:\n"
                        f"{prompt_text}\n\n"
                        "On-screen content (do not read verbatim if long; summarise):\n"
                        f"{screen_text}\n"
                    )

                    narration_response = _llm_generate_spoken_backfill(
                        llm_client, narration_system, narration_user, model_name
                    )
                    spoken_backfill_source = "llm_synthesis"
                    spoken_backfill_model_id = model_name

                    spoken_fallback = _coerce_spoken_text(narration_response)
                    if not spoken_fallback:
                        spoken_fallback = _coerce_spoken_text(screen_text)
                        spoken_from_screen_text = bool(spoken_fallback)
                        if spoken_from_screen_text:
                            spoken_backfill_source = "screen_text_fallback"

                    if spoken_fallback:
                        base_channels = (
                            dict(presenter_channels)
                            if isinstance(presenter_channels, dict)
                            else {}
                        )
                        base_channels["screen"] = screen_text
                        base_channels["spoken"] = spoken_fallback
                        existing_format = base_channels.get("format")
                        # Preserve formats that describe *how the screen* was produced.
                        # For normal tagged responses, surface that narration was added.
                        if existing_format == "tool_results_fallback_v1":
                            base_channels["format"] = existing_format
                        elif (
                            has_tool_messages
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = existing_format
                        elif (
                            not has_tool_messages
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = "narration_fallback_v1"
                        elif (
                            spoken_backfill_second_pass_reason
                            == "missing_presenter_channels"
                        ):
                            base_channels["format"] = "narration_fallback_v1"
                        elif (
                            spoken_from_screen_text
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = existing_format
                        else:
                            base_channels["format"] = "narration_fallback_v1"
                        presenter_channels = base_channels
                        spoken_backfill_applied = True
            except Exception as exc:
                # Defensive: never fail the request just because narration generation failed.
                spoken_backfill_error_class = type(exc).__name__
                presenter_channels = presenter_channels

        spoken_backfill_latency_ms = (
            time.perf_counter() - spoken_backfill_started_perf
        ) * 1000.0
        if not presenter_mode_requested:
            spoken_backfill_status = "skipped"
            spoken_backfill_suppression_reason = "presenter_mode_disabled"
        elif not needs_spoken_backfill:
            spoken_backfill_status = "skipped"
            spoken_backfill_suppression_reason = "not_required"
        elif spoken_backfill_applied:
            spoken_backfill_status = (
                "fallback_success"
                if spoken_backfill_source == "screen_text_fallback"
                else "success"
            )
            spoken_backfill_suppression_reason = None
        elif spoken_backfill_error_class:
            spoken_backfill_status = "failure"
            spoken_backfill_suppression_reason = "model_error"
        else:
            spoken_backfill_status = "no_op"
            spoken_backfill_suppression_reason = (
                spoken_backfill_second_pass_reason or "no_spoken_generated"
            )

        record_response_transformation_event(
            response_transformations,
            event=build_response_transformation_event(
                transform_name="spoken_backfill",
                transform_version="v1",
                status=spoken_backfill_status,
                input_summary={
                    "presenter_mode_requested": presenter_mode_requested,
                    "needs_backfill": needs_spoken_backfill,
                    "reason": spoken_backfill_second_pass_reason,
                    "tool_message_count": len(tool_messages),
                },
                output_summary={
                    "applied": spoken_backfill_applied,
                    "source": spoken_backfill_source,
                    "spoken_from_screen_text": spoken_from_screen_text,
                    "presenter_format": (
                        presenter_channels.get("format")
                        if isinstance(presenter_channels, dict)
                        else None
                    ),
                },
                options_emitted_count=0,
                source_path=spoken_backfill_source,
                latency_ms=spoken_backfill_latency_ms,
                model_id=spoken_backfill_model_id,
                suppression_reason=spoken_backfill_suppression_reason,
                error_class=spoken_backfill_error_class,
            ),
        )

        if presenter_channels is not None:
            # Screen channel becomes the stored/displayed response.
            # Exception: tool-results fallback is debug-only; do not show it as the main chat bubble.
            presenter_format = presenter_channels.get("format")
            screen_text_value = presenter_channels.get("screen")

            if presenter_format == "tool_results_fallback_v1":
                # Keep the model's user-facing response when available.
                # Only strip <spoken> tags if present, or fall back to spoken when the
                # response is empty.
                extracted = _extract_spoken_only(response_text)
                if extracted:
                    response_text = extracted
                elif isinstance(response_text, str) and response_text.strip():
                    pass
                else:
                    spoken_value = presenter_channels.get("spoken")
                    if isinstance(spoken_value, str) and spoken_value.strip():
                        response_text = spoken_value.strip()
            else:
                if isinstance(screen_text_value, str) and screen_text_value.strip():
                    response_text = screen_text_value.strip()

        if tool_invocations:
            current_app.logger.info(
                "[mcp_orchestrator] Tool invocations: %s", tool_invocations
            )

        render_plan_for_display = (
            render_plan_debug if isinstance(render_plan_debug, dict) else None
        )
        screen_element_targets = _extract_screen_element_targets_from_render_plan(
            render_plan_for_display
        )
        screen_element_reason_codes = (
            _extract_screen_element_reason_codes_from_render_plan(
                render_plan_for_display
            )
        )
        screen_table_elements = _extract_screen_table_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_workflow_elements = _extract_screen_workflow_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_task_view_elements = _extract_screen_task_view_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_calendar_elements = _extract_screen_calendar_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_chart_elements = _extract_screen_chart_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_location_elements = _extract_screen_location_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_document_elements = _extract_screen_document_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_kanban_elements = _extract_screen_kanban_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_timeline_elements = _extract_screen_timeline_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_hierarchy_elements = _extract_screen_hierarchy_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_relation_graph_elements = (
            _extract_screen_relation_graph_elements_from_render_plan(
                render_plan_for_display,
                screen_element_targets=screen_element_targets,
            )
        )
        screen_relation_truth_state_elements = (
            _extract_screen_relation_truth_state_elements_from_render_plan(
                render_plan_for_display,
                screen_element_targets=screen_element_targets,
            )
        )
        display_elements_contract = build_turn_display_elements(
            response_text=response_text,
            presenter_channels=(
                presenter_channels if isinstance(presenter_channels, dict) else None
            ),
            screen_table_elements=screen_table_elements,
            screen_workflow_elements=screen_workflow_elements,
            screen_task_view_elements=screen_task_view_elements,
            screen_calendar_elements=screen_calendar_elements,
            screen_chart_elements=screen_chart_elements,
            screen_location_elements=screen_location_elements,
            screen_document_elements=screen_document_elements,
            screen_kanban_elements=screen_kanban_elements,
            screen_timeline_elements=screen_timeline_elements,
            screen_hierarchy_elements=screen_hierarchy_elements,
            screen_relation_graph_elements=screen_relation_graph_elements,
            screen_relation_truth_state_elements=screen_relation_truth_state_elements,
            supplemental_reason_codes=screen_element_reason_codes,
            required_screen_json_fence=required_screen_json_fence,
            screen_backfill_second_pass_attempted=screen_backfill_second_pass_attempted,
            screen_backfill_second_pass_reason=screen_backfill_second_pass_reason,
            spoken_backfill_second_pass_attempted=spoken_backfill_second_pass_attempted,
            spoken_backfill_second_pass_reason=spoken_backfill_second_pass_reason,
        )

        # Calculate context statistics for visibility.
        # If the internal orchestrator is enabled, it augments and trims the context
        # before sending it to the LLM, so report stats for the *actual* sent context.
        sent_context_for_stats = enhanced_context
        if orchestrator is not None:
            build_augmented = getattr(orchestrator, "_build_augmented_context", None)
            if callable(build_augmented):
                try:
                    sent_context_for_stats = build_augmented(
                        orchestrator_input_context,
                        user_namespace=user_namespace,
                        auxiliary_system_prompt=dynamic_instructions,
                        user_concept_id=user_concept_id,
                        org_concept_id=org_concept_id,
                    )
                except Exception:
                    sent_context_for_stats = enhanced_context

        sent_context_stats_messages: list[dict] = enhanced_context
        if isinstance(sent_context_for_stats, list):
            normalised_messages: list[dict] = []
            for msg in sent_context_for_stats:
                if isinstance(msg, dict):
                    normalised_messages.append(msg)
                    continue
                try:
                    normalised_messages.append(dict(msg))
                except Exception:
                    continue
            if normalised_messages:
                sent_context_stats_messages = normalised_messages

        buttonify_started_perf = time.perf_counter()
        buttonify_options: list[str] = []
        buttonify_meta: dict[str, Any] | None = None
        buttonify_workflow_contract: dict[str, Any] | None = None
        agent_test_instance = str(
            os.getenv("VON_AGENT_TEST_INSTANCE") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        buttonify_setting_enabled = get_buttonify_model_enabled()
        buttonify_enabled = (
            buttonify_setting_enabled and not skip_buttonify and not agent_test_instance
        )
        buttonify_allowed = (
            not current_app.testing
            and not os.getenv("PYTEST_CURRENT_TEST")
            and (
                orchestrator is None or hasattr(orchestrator, "_run_llm_with_fallbacks")
            )
        )
        buttonify_workflow_available = bool(
            orchestrator is not None
            and hasattr(orchestrator, "execute_workflow")
            and hasattr(orchestrator, "_run_llm_with_fallbacks")
        )
        buttonify_model_used = model_name
        buttonify_source = "none"
        buttonify_prompt_id = None
        buttonify_prompt_truncated = False
        buttonify_prompt_available = False
        buttonify_prompt_error = None
        buttonify_status = "skipped"
        buttonify_error_class = None
        buttonify_suppression_reason = None
        buttonify_model_attempted = False
        buttonify_workflow_used = False
        buttonify_filtering_boundary: dict[str, Any] | None = None

        if skip_buttonify:
            buttonify_suppression_reason = "buttonify_skipped_by_request"
        elif agent_test_instance:
            buttonify_suppression_reason = "agent_test_instance"
        elif not buttonify_enabled:
            buttonify_suppression_reason = "buttonify_disabled"
        elif not buttonify_allowed:
            buttonify_suppression_reason = "buttonify_not_allowed"
        elif not isinstance(response_text, str) or not response_text.strip():
            buttonify_suppression_reason = "empty_response"
        else:
            buttonify_status = "no_op"
            if show_tool_use_progress:
                _emit_generate_progress(
                    {
                        "status": "workflow",
                        "request_id": request_id,
                        "workflow_task": "buttonify",
                    },
                )

            if buttonify_workflow_available:
                buttonify_workflow_used = True
                buttonify_workflow_result = None
                policy_state = None
                registry_snapshot = None
                try:
                    policy_state, registry_snapshot = (
                        orchestrator._load_workflow_model_policy(request_language)
                    )
                except Exception:
                    policy_state = None
                    registry_snapshot = None

                try:
                    buttonify_workflow_result = orchestrator.execute_workflow(
                        CHAT_BUTTONIFY_WORKFLOW_ID,
                        data={
                            "screen_text": response_text,
                            "user_prompt": prompt_text,
                            "buttonify_enabled": buttonify_enabled,
                            "buttonify_allowed": buttonify_allowed,
                            "buttonify_prompt_ids": list(BUTTONIFY_PROMPT_IDS),
                            "prefer_default_model": True,
                            "default_model": model_name,
                            "policy_state": policy_state,
                            "registry_snapshot": registry_snapshot,
                            "record_llm_call": _record_stage_llm_call,
                            "emit_progress": _emit_stage_progress,
                            "llm_calls_log": llm_interaction["calls"],
                            "aux_llm_calls": auxiliary_llm_calls,
                            "user_concept_id": user_concept_id,
                            "org_concept_id": org_concept_id,
                            "workflow_episode_stage": "buttonify",
                        },
                        llm_client=llm_client,
                        model=model_name,
                        user_namespace=user_namespace,
                        auxiliary_system_prompt=dynamic_instructions,
                        trace=None,
                        conversation_session_id=session_id,
                        turn_id=request_id,
                        episode_source="chat_turn_workflow",
                    )
                except Exception as exc:
                    buttonify_error_class = type(exc).__name__
                    buttonify_workflow_result = None

                workflow_payload: Mapping[str, Any] = {}
                if buttonify_workflow_result is not None and isinstance(
                    buttonify_workflow_result.data, Mapping
                ):
                    workflow_payload = buttonify_workflow_result.data

                raw_options = workflow_payload.get("buttonify_options")
                if isinstance(raw_options, list):
                    (
                        buttonify_options,
                        buttonify_filtering_boundary,
                    ) = sanitise_buttonify_options_with_telemetry(raw_options)

                if isinstance(workflow_payload.get("buttonify_source"), str):
                    buttonify_source = workflow_payload.get("buttonify_source", "none")
                if isinstance(workflow_payload.get("buttonify_prompt_id"), str):
                    buttonify_prompt_id = workflow_payload.get("buttonify_prompt_id")
                buttonify_prompt_truncated = bool(
                    workflow_payload.get("buttonify_prompt_truncated")
                )
                if isinstance(workflow_payload.get("buttonify_prompt_available"), bool):
                    buttonify_prompt_available = bool(
                        workflow_payload.get("buttonify_prompt_available")
                    )
                if isinstance(workflow_payload.get("buttonify_prompt_error"), str):
                    buttonify_prompt_error = workflow_payload.get(
                        "buttonify_prompt_error"
                    )
                if isinstance(workflow_payload.get("buttonify_status"), str):
                    buttonify_status = workflow_payload.get("buttonify_status", "no_op")
                if isinstance(workflow_payload.get("buttonify_error_class"), str):
                    buttonify_error_class = workflow_payload.get(
                        "buttonify_error_class"
                    )
                if isinstance(workflow_payload.get("buttonify_model_used"), str):
                    buttonify_model_used = workflow_payload.get("buttonify_model_used")
                buttonify_model_attempted = bool(
                    workflow_payload.get("buttonify_model_attempted")
                )
                if isinstance(
                    workflow_payload.get("buttonify_suppression_reason"), str
                ):
                    buttonify_suppression_reason = workflow_payload.get(
                        "buttonify_suppression_reason"
                    )
                contract_payload = workflow_payload.get(
                    "output_transformation_contract"
                )
                if isinstance(contract_payload, Mapping):
                    buttonify_workflow_contract = dict(contract_payload)

            else:
                # Keep deterministic behaviour when workflow execution is not available.
                # If Vontology prompt content is unavailable, buttonify must no-op
                # rather than silently falling back to code prompt text.
                buttonify_prompt = ""
                try:
                    rendered_prompt = PromptTemplateService().render_prompt(
                        BUTTONIFY_PROMPT_IDS,
                        variables={
                            "user_message": prompt_text,
                            "assistant_response": response_text,
                        },
                        fallback=None,
                        max_chars=6000,
                    )
                except Exception as exc:
                    rendered_prompt = None
                    buttonify_prompt_error = type(exc).__name__

                if rendered_prompt is not None:
                    buttonify_prompt = enforce_buttonify_prompt_contract(
                        rendered_prompt.text
                    )
                    buttonify_prompt_id = rendered_prompt.prompt_id
                    buttonify_prompt_truncated = rendered_prompt.truncated
                    buttonify_prompt_available = True
                else:
                    buttonify_prompt_available = False
                    if not buttonify_prompt_error:
                        buttonify_prompt_error = "prompt_not_found_or_unavailable"
                    buttonify_suppression_reason = "buttonify_prompt_unavailable"

                if buttonify_prompt_available and not buttonify_options:
                    buttonify_response = None
                    if _contains_openai_quota_error(response_text):
                        buttonify_suppression_reason = "quota_exhausted"
                    else:
                        buttonify_model_attempted = True
                        llm_start = time.perf_counter()
                        try:
                            buttonify_response = _llm_generate_buttonify(
                                llm_client, buttonify_prompt, buttonify_model_used
                            )
                            _record_stage_llm_call(
                                call_type="llm.generate",
                                model_name=buttonify_model_used,
                                duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                                usage=None,
                                note="Buttonify quick-reply extraction (workflow unavailable).",
                                stage="buttonify",
                                prompt={
                                    "text": buttonify_prompt,
                                    "char_count": len(buttonify_prompt),
                                    "is_truncated": False,
                                },
                                response={
                                    "text": str(buttonify_response),
                                    "char_count": len(str(buttonify_response)),
                                    "is_truncated": False,
                                },
                            )
                        except Exception as exc:
                            buttonify_error_class = type(exc).__name__
                            if _contains_openai_quota_error(exc):
                                buttonify_suppression_reason = "quota_exhausted"
                            buttonify_response = None

                    (
                        buttonify_options,
                        buttonify_filtering_boundary,
                    ) = parse_buttonify_options_json_with_telemetry(buttonify_response)
                    buttonify_source = "llm" if buttonify_options else "none"

            if buttonify_source == "llm":
                buttonify_status = "success"
            else:
                buttonify_status = "no_op"
                if buttonify_suppression_reason:
                    pass
                elif buttonify_error_class:
                    buttonify_suppression_reason = "model_error"
                elif not buttonify_prompt_available:
                    buttonify_suppression_reason = "buttonify_prompt_unavailable"
                elif not buttonify_suppression_reason:
                    buttonify_suppression_reason = "no_candidates"

            buttonify_meta = {
                "enabled": True,
                "model": buttonify_model_used,
                "status": buttonify_status,
                "suppression_reason": buttonify_suppression_reason,
                "options": buttonify_options,
                "source": buttonify_source,
                "prompt_id": buttonify_prompt_id,
                "prompt_truncated": buttonify_prompt_truncated,
                "prompt_available": buttonify_prompt_available,
                "prompt_error": buttonify_prompt_error,
                "workflow_used": buttonify_workflow_used,
                "workflow_available": buttonify_workflow_available,
            }
            if isinstance(buttonify_filtering_boundary, Mapping):
                buttonify_meta["filtering_boundary"] = dict(
                    buttonify_filtering_boundary
                )
            if buttonify_workflow_contract is not None:
                buttonify_meta["workflow_contract"] = buttonify_workflow_contract

        buttonify_latency_ms = (time.perf_counter() - buttonify_started_perf) * 1000.0
        record_response_transformation_event(
            response_transformations,
            event=build_response_transformation_event(
                transform_name="buttonify",
                transform_version="v1",
                status=buttonify_status,
                input_summary={
                    "buttonify_enabled": buttonify_enabled,
                    "buttonify_allowed": buttonify_allowed,
                    "response_text_chars": (
                        len(response_text) if isinstance(response_text, str) else 0
                    ),
                },
                output_summary={
                    "options": list(buttonify_options),
                    "source": buttonify_source,
                    "prompt_id": buttonify_prompt_id,
                    "prompt_truncated": buttonify_prompt_truncated,
                    "prompt_available": buttonify_prompt_available,
                    "prompt_error": buttonify_prompt_error,
                    "filtering_boundary": (
                        dict(buttonify_filtering_boundary)
                        if isinstance(buttonify_filtering_boundary, Mapping)
                        else None
                    ),
                },
                options_emitted_count=len(buttonify_options),
                source_path=buttonify_source,
                latency_ms=buttonify_latency_ms,
                model_id=buttonify_model_used if buttonify_model_attempted else None,
                suppression_reason=buttonify_suppression_reason,
                error_class=buttonify_error_class,
            ),
        )

        context_stats = _calculate_context_stats(sent_context_stats_messages)

        # stored_context should reflect the persisted user/session history when authenticated,
        # not the unauthenticated in-memory CONTEXT list.
        if user_concept_id:
            try:
                persisted_history = chat_history_service.get_chat_history(
                    user_concept_id,
                    session_id,
                    namespace=(
                        user_namespace
                        if isinstance(user_namespace, str) and user_namespace.strip()
                        else None
                    ),
                )
            except Exception:
                persisted_history = []
            current_context_stats = _calculate_context_stats(persisted_history)
        else:
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )

        # Calculate tool statistics if tools were used
        tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

        # JVNAUTOSCI-958: cartouche-style concept metadata for references that appear
        # in the context actually sent to the model (prior user/assistant turns).
        # This makes reference status inspectable in the ensuing turn's debug payload.
        try:
            context_concept_references = build_context_concept_reference_metadata(
                sent_context_stats_messages,
                source="sent_context_user_assistant",
            )
        except Exception as exc:
            current_app.logger.debug(
                "Failed to build context concept-reference metadata: %s",
                exc,
                exc_info=True,
            )
            context_concept_references = {
                "source": "sent_context_user_assistant",
                "metadata_version": 1,
                "message_roles": ["user", "assistant"],
                "messages_scanned": 0,
                "concept_count": 0,
                "concept_count_capped": False,
                "max_concepts": None,
                "include_direct_supertypes": False,
                "max_direct_supertypes": 0,
                "concepts": [],
                "error": str(exc),
            }

        # Capture a compact summary of the tool catalogue that the agent was shown.
        # This improves trace transparency without storing the full system prompt.
        tool_catalogue_summary = None
        if gateway is not None:
            try:
                methods_snapshot = gateway.describe_methods()
                if isinstance(methods_snapshot, dict):
                    method_names = sorted(
                        [
                            name
                            for name in methods_snapshot.keys()
                            if isinstance(name, str)
                        ]
                    )
                    try:
                        import hashlib
                        import json

                        digest = hashlib.sha256(
                            json.dumps(
                                method_names,
                                separators=(",", ":"),
                                ensure_ascii=True,
                            ).encode("utf-8")
                        ).hexdigest()
                    except Exception:
                        digest = None

                    sample_cap = 25
                    tool_catalogue_summary = {
                        "method_count": len(method_names),
                        "sha256": digest,
                        "sample": method_names[:sample_cap],
                        "sample_truncated": len(method_names) > sample_cap,
                    }
            except Exception:
                tool_catalogue_summary = None

        try:
            applied_max_tool_invocations = int(get_internal_mcp_max_tool_invocations())
        except Exception:
            applied_max_tool_invocations = None

        try:
            applied_tool_batch_cap = int(get_internal_mcp_tool_batch_cap())
        except Exception:
            applied_tool_batch_cap = None

        if show_tool_use_progress:
            response_finalising_payload = (
                _build_response_finalising_tool_progress_payload(
                    request_id=request_id,
                    eta_ms=_estimate_response_finalising_eta_ms(
                        response_text=response_text,
                        tool_message_count=len(tool_messages),
                        persist_history=bool(history_user_id),
                    ),
                    response_text=response_text,
                    tool_message_count=len(tool_messages),
                    persist_history=bool(history_user_id),
                )
            )
            with turn_timing_recorder.span(
                stage_id="response_finalising",
                operation_kind="progress_update",
                operation_name="emit_response_finalising_progress",
            ):
                _emit_generate_progress(response_finalising_payload)

        with turn_timing_recorder.span(
            stage_id="response_finalising",
            operation_kind="progress_snapshot",
            operation_name="snapshot_tool_progress_for_diagnostics",
        ):
            tool_progress_snapshot = _snapshot_tool_progress_for_request(
                progress_scope_key, request_id
            )
        with turn_timing_recorder.span(
            stage_id="response_finalising",
            operation_kind="mcp_access_metadata",
            operation_name="build_turn_execution_mcp_access",
        ):
            turn_execution_mcp_access = _build_turn_execution_mcp_access(
                request_id=request_id,
                session_id=session_id,
                namespace=user_namespace,
                history_owner_user_id=history_user_id,
                organisation_concept_id=org_concept_id,
                workflow_routing=workflow_routing_info,
                aux_llm_calls=auxiliary_llm_calls,
                selected_workflow_trace=selected_workflow_trace_payload,
            )
        with turn_timing_recorder.span(
            stage_id="response_finalising",
            operation_kind="diagnostics_assembly",
            operation_name="build_turn_execution_diagnostics",
        ):
            turn_execution_diagnostics = _build_turn_execution_diagnostics(
                request_id=request_id,
                prompt_text=prompt_text,
                elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
                tool_progress_state=tool_progress_snapshot,
                workflow_discovery=workflow_discovery_result,
                workflow_routing=workflow_routing_info,
                llm_calls=llm_interaction["calls"],
                aux_llm_calls=auxiliary_llm_calls,
                tool_invocations=tool_invocations,
                timing_spans=turn_timing_recorder.spans(),
                response_transformations=(
                    response_transformations
                    if "response_transformations" in locals()
                    and isinstance(response_transformations, Mapping)
                    else None
                ),
                critic_verdict=(
                    dict(raw_critic_verdict)
                    if isinstance(
                        raw_critic_verdict := getattr(
                            orchestrator_result,
                            "critic_verdict",
                            None,
                        ),
                        Mapping,
                    )
                    else None
                ),
                completion_gate_verdict=(
                    dict(raw_completion_gate_verdict)
                    if isinstance(
                        raw_completion_gate_verdict := getattr(
                            orchestrator_result,
                            "completion_gate_verdict",
                            None,
                        ),
                        Mapping,
                    )
                    else None
                ),
                mcp_access=turn_execution_mcp_access,
            )
        diagnostics_routing = turn_execution_diagnostics.get(
            "workflow_routing_diagnostics"
        )
        diagnostics_dispatch = (
            diagnostics_routing.get("dispatch")
            if isinstance(diagnostics_routing, Mapping)
            and isinstance(diagnostics_routing.get("dispatch"), Mapping)
            else {}
        )
        try:
            diagnostics_unsatisfied_required_tools = int(
                diagnostics_dispatch.get(
                    "required_tool_obligation_unsatisfied_count",
                    0,
                )
                or 0
            )
        except (TypeError, ValueError):
            diagnostics_unsatisfied_required_tools = 0
        if (
            isinstance(response_text, str)
            and diagnostics_dispatch
            and not diagnostics_dispatch.get("required_effects_unresolved_effect_ids")
            and diagnostics_unsatisfied_required_tools == 0
        ):
            response_text = strip_completion_ledger_suffix(response_text)

        workflow_use_episodes = [
            entry
            for entry in auxiliary_llm_calls
            if isinstance(entry, dict)
            and str(entry.get("type", "")).strip() == "workflow_use_episode"
        ]
        turn_record_tool_invocations = (
            _serialise_tool_invocations_for_turn_execution_record(tool_invocations)
        )
        search_evidence = build_search_tool_evidence(tool_invocations)

        llm_debug_info = {
            "interaction_timestamp_utc": interaction_timestamp_utc,
            "request_id": request_id,
            "model": model_name,
            "llm_interaction": {
                **llm_interaction,
                "server_elapsed_ms": (time.perf_counter() - request_start_perf)
                * 1000.0,
            },
            "messages": current_turn_messages,
            "response": response_text,
            "presenter_channels": presenter_channels,
            "screen_backfill_second_pass_attempted": screen_backfill_second_pass_attempted,
            "screen_backfill_second_pass_reason": screen_backfill_second_pass_reason,
            "display_elements_screen_fence_compat_enabled": (
                screen_fence_compat_enabled if presenter_mode_requested else None
            ),
            "spoken_backfill_second_pass_attempted": spoken_backfill_second_pass_attempted,
            "spoken_backfill_second_pass_reason": spoken_backfill_second_pass_reason,
            "user_prompt": user_prompt_debug,
            "namespace_report": namespace_report,
            "internal_mcp": {
                "gateway_present": gateway is not None,
                "gateway_enabled": (
                    bool(getattr(gateway, "enabled", False))
                    if gateway is not None
                    else False
                ),
                "orchestrator_present": orchestrator is not None,
                "execution_caps": {
                    "max_tool_invocations": applied_max_tool_invocations,
                    "tool_batch_cap": applied_tool_batch_cap,
                },
                "tool_use_progress": {
                    "enabled": show_tool_use_progress,
                    "request_id": request_id,
                    "scope_keys": _normalise_tool_progress_scope_keys(
                        [progress_scope_key, *progress_mirror_scope_keys]
                    ),
                    "diagnostic_summary": _build_tool_progress_compact_summary(
                        tool_progress_snapshot
                    ),
                },
                "tool_catalogue": tool_catalogue_summary,
            },
            "context_stats": {
                "sent_to_llm": context_stats,  # What was actually sent this turn
                "stored_context": current_context_stats,  # Current state after this turn
            },
            "context_concept_references": context_concept_references,
            "tool_stats": tool_stats,  # MCP tool result statistics
            "tool_invocations": serialised_tool_invocations,
            "turn_execution_record_tool_invocations": turn_record_tool_invocations,
            "search_evidence": search_evidence,
            "aux_llm_calls": auxiliary_llm_calls,
            "workflow_use_episodes": workflow_use_episodes,
            "buttonify": buttonify_meta,
            "response_transformations": response_transformations,
            # JVNAUTOSCI-1076: Workflow discovery results for Thinking context
            "workflow_discovery": workflow_discovery_result,
            "workflow_routing": workflow_routing_info,
            "display_elements": display_elements_contract,
            "turn_execution_diagnostics": turn_execution_diagnostics,
            "selected_workflow_trace": selected_workflow_trace_payload,
            "critic_verdict": (
                dict(raw_critic_verdict)
                if isinstance(
                    raw_critic_verdict := getattr(
                        orchestrator_result,
                        "critic_verdict",
                        None,
                    ),
                    dict,
                )
                else None
            ),
            "completion_gate_verdict": (
                dict(raw_completion_gate_verdict)
                if isinstance(
                    raw_completion_gate_verdict := getattr(
                        orchestrator_result,
                        "completion_gate_verdict",
                        None,
                    ),
                    dict,
                )
                else None
            ),
            "completion_report": (
                dict(raw_completion_report)
                if isinstance(
                    raw_completion_report := getattr(
                        orchestrator_result,
                        "completion_report",
                        None,
                    ),
                    dict,
                )
                else None
            ),
        }
        llm_debug_info["turn_output_health"] = build_turn_output_health(llm_debug_info)
        if isinstance(render_plan_debug, dict):
            llm_debug_info["render_plan"] = dict(render_plan_debug)

        with turn_timing_recorder.span(
            stage_id="response_finalising",
            operation_kind="debug_record_assembly",
            operation_name="finalise_llm_debug_info",
        ):
            llm_debug_info = _finalise_llm_debug_info(
                llm_debug_info=llm_debug_info,
                prompt_text=prompt_text,
                response_text=response_text,
                session_id=session_id,
                namespace=user_namespace,
                user_id=history_user_id or user_concept_id,
                org_id=org_concept_id,
                workflow_discovery=workflow_discovery_result,
                workflow_routing=workflow_routing_info,
            )

        def _refresh_llm_debug_timing_payload(debug_payload: dict[str, Any]) -> None:
            diagnostics_payload = debug_payload.get("turn_execution_diagnostics")
            if not isinstance(diagnostics_payload, dict):
                return
            trace = build_turn_timing_trace(
                request_id=request_id,
                phase_history=(
                    diagnostics_payload.get("phase_history")
                    if isinstance(diagnostics_payload.get("phase_history"), list)
                    else []
                ),
                diagnostic_events=(
                    diagnostics_payload.get("latest_progress", {}).get(
                        "diagnostic_events"
                    )
                    if isinstance(diagnostics_payload.get("latest_progress"), Mapping)
                    and isinstance(
                        diagnostics_payload.get("latest_progress", {}).get(
                            "diagnostic_events"
                        ),
                        list,
                    )
                    else []
                ),
                llm_calls=(
                    diagnostics_payload.get("llm_calls")
                    if isinstance(diagnostics_payload.get("llm_calls"), list)
                    else []
                ),
                tool_history=(
                    diagnostics_payload.get("tool_history")
                    if isinstance(diagnostics_payload.get("tool_history"), list)
                    else []
                ),
                tool_invocations=turn_record_tool_invocations,
                response_transformations=(
                    diagnostics_payload.get("response_transformations")
                    if isinstance(
                        diagnostics_payload.get("response_transformations"), Mapping
                    )
                    else None
                ),
                extra_spans=turn_timing_recorder.spans(),
                elapsed_ms_value=(
                    int(diagnostics_payload.get("elapsed_ms"))
                    if isinstance(diagnostics_payload.get("elapsed_ms"), (int, float))
                    and not isinstance(diagnostics_payload.get("elapsed_ms"), bool)
                    else None
                ),
            )
            diagnostics_payload["turn_timing_trace"] = trace
            diagnostics_payload["timing_breakdown"] = (
                _attach_turn_timing_trace_to_breakdown(
                    diagnostics_payload.get("timing_breakdown"),
                    trace,
                )
            )
            turn_record_payload = debug_payload.get("turn_execution_record")
            if isinstance(turn_record_payload, dict):
                turn_record_payload["turn_execution_diagnostics"] = diagnostics_payload
                turn_record_payload["timing_breakdown"] = diagnostics_payload.get(
                    "timing_breakdown"
                )

        _refresh_llm_debug_timing_payload(llm_debug_info)

        background_success_body = _build_generate_success_body(
            request_id=request_id,
            session_id=session_id,
            created_conversation_session_name=created_conversation_session_name,
            created_conversation_session=created_conversation_session,
            response_text=response_text,
            presenter_channels=(
                presenter_channels if isinstance(presenter_channels, dict) else None
            ),
            llm_debug_info=llm_debug_info,
            display_elements_contract=display_elements_contract,
            rag_trace=rag_trace,
        )
        _mark_background_generate_completed_if_ready(
            background_task_id=background_task_id,
            result_body=background_success_body,
            request_id=request_id,
            session_id=session_id,
            user_id=history_user_id or user_concept_id,
            response_text=response_text,
        )

        current_app.config["CONTEXT"] = _persist_generate_turn_messages(
            history_user_id=history_user_id,
            user_message_persisted_early=_user_message_persisted_early,
            prompt_text=prompt_text,
            user_concept_id=user_concept_id,
            session_id=session_id,
            tool_messages=tool_messages,
            response_text=response_text,
            llm_debug_info=llm_debug_info,
            user_namespace=user_namespace,
            org_concept_id=org_concept_id,
            role_in_org=role_in_org,
            current_context=current_app.config.get("CONTEXT", []),
            truncate_large_tool_results_fn=_truncate_large_tool_results,
            add_chat_history_message_fn=_add_chat_history_message,
            limit_context_size_fn=_limit_context_size,
            timing_recorder=turn_timing_recorder,
            refresh_llm_debug_timing_fn=_refresh_llm_debug_timing_payload,
        )

        with turn_timing_recorder.span(
            stage_id="response_finalising",
            operation_kind="durable_instance_finalisation",
            operation_name="finalise_conversation_turn_instance",
        ):
            _finalise_generate_conversation_turn_instance(
                state=conversation_turn_instance_state,
                auxiliary_llm_calls=auxiliary_llm_calls,
                session_id=session_id,
                request_id=request_id,
                user_namespace=user_namespace,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                presenter_mode_requested=presenter_mode_requested,
                completed=True,
                final_state="completed",
                debug_payload=llm_debug_info,
                logger=current_app.logger,
            )
        _refresh_llm_debug_timing_payload(llm_debug_info)

        if show_tool_use_progress:
            turn_record_completion_gate = None
            turn_execution_record = llm_debug_info.get("turn_execution_record")
            if isinstance(turn_execution_record, Mapping):
                raw_gate = turn_execution_record.get("completion_gate")
                if isinstance(raw_gate, Mapping):
                    turn_record_completion_gate = raw_gate
            final_progress_payload = _build_terminal_tool_progress_payload(
                request_id=request_id,
                aux_calls=auxiliary_llm_calls,
                completion_gate=turn_record_completion_gate,
            )
            final_progress_payload["timing_spans"] = turn_timing_recorder.spans()
            with turn_timing_recorder.span(
                stage_id="response_finalising",
                operation_kind="progress_update",
                operation_name="stop_progress_heartbeat",
            ):
                _stop_tool_progress_heartbeat(
                    progress_heartbeat_stop_event, progress_heartbeat_thread
                )
            final_progress_payload["timing_spans"] = turn_timing_recorder.spans()
            with turn_timing_recorder.span(
                stage_id="response_finalising",
                operation_kind="progress_update",
                operation_name="emit_terminal_progress",
            ):
                _emit_generate_progress(final_progress_payload)
        _refresh_llm_debug_timing_payload(llm_debug_info)
        return jsonify(
            _build_generate_success_body(
                request_id=request_id,
                session_id=session_id,
                created_conversation_session_name=created_conversation_session_name,
                created_conversation_session=created_conversation_session,
                response_text=response_text,
                presenter_channels=(
                    presenter_channels if isinstance(presenter_channels, dict) else None
                ),
                llm_debug_info=llm_debug_info,
                display_elements_contract=display_elements_contract,
                rag_trace=rag_trace,
            )
        )
    except CancellationRequested:
        if progress_updates_enabled:
            try:
                if show_tool_use_progress:
                    _stop_tool_progress_heartbeat(
                        progress_heartbeat_stop_event, progress_heartbeat_thread
                    )
                _emit_generate_progress(
                    {
                        "status": "cancelled",
                        "phase": "cancelled",
                        "phase_label": "Cancelled",
                        "request_id": request_id,
                        "result_summary": "Cancellation requested for the background generate task.",
                    }
                )
            except Exception:
                pass
        _persist_terminal_failure_turn_record_best_effort(
            local_vars=locals(),
            terminal_status="cancelled",
            error_text="generate_task_cancelled",
            error_class="CancellationRequested",
        )
        raise
    except Exception as e:
        print(f"Error during generation: {e}")  # Log error server-side
        # Return error with debug info showing the current turn only (not full context)
        # to avoid exponential token growth in debug data

        if "progress_updates_enabled" in locals() and progress_updates_enabled:
            try:
                if "show_tool_use_progress" in locals() and show_tool_use_progress:
                    _stop_tool_progress_heartbeat(
                        progress_heartbeat_stop_event, progress_heartbeat_thread
                    )
                _emit_generate_progress(
                    {
                        "status": "error",
                        "request_id": (
                            request_id if "request_id" in locals() else "unknown"
                        ),
                        "error": str(e),
                    }
                )
            except Exception:
                pass

        error_tool_progress_snapshot = _snapshot_tool_progress_for_request(
            progress_scope_key if "progress_scope_key" in locals() else None,
            request_id if "request_id" in locals() else None,
        )
        error_workflow_discovery = (
            workflow_discovery_result
            if (
                "workflow_discovery_result" in locals()
                and isinstance(workflow_discovery_result, dict)
            )
            else None
        )
        error_elapsed_ms = (
            (time.perf_counter() - request_start_perf) * 1000.0
            if "request_start_perf" in locals()
            else None
        )
        error_workflow_routing = (
            workflow_routing_info
            if (
                "workflow_routing_info" in locals()
                and isinstance(workflow_routing_info, dict)
            )
            else None
        )
        error_debug_info = _build_generate_error_debug_info(
            interaction_timestamp_utc=interaction_timestamp_utc,
            request_id=request_id if "request_id" in locals() else None,
            model_name=model_name if "model_name" in locals() else None,
            prompt_text=prompt_text if "prompt_text" in locals() else None,
            error_text=str(e),
            enhanced_context=(
                enhanced_context
                if "enhanced_context" in locals() and isinstance(enhanced_context, list)
                else None
            ),
            user_prompt_debug=(
                user_prompt_debug
                if "user_prompt_debug" in locals()
                and isinstance(user_prompt_debug, Mapping)
                else None
            ),
            response_transformations=(
                response_transformations
                if "response_transformations" in locals()
                and isinstance(response_transformations, Mapping)
                else None
            ),
            error_tool_progress_snapshot=error_tool_progress_snapshot,
            error_workflow_discovery=error_workflow_discovery,
            error_workflow_routing=error_workflow_routing,
            auxiliary_llm_calls=(
                auxiliary_llm_calls
                if "auxiliary_llm_calls" in locals()
                and isinstance(auxiliary_llm_calls, list)
                else None
            ),
            error_elapsed_ms=error_elapsed_ms,
            session_id=session_id if "session_id" in locals() else None,
            namespace=user_namespace if "user_namespace" in locals() else None,
            history_user_id=history_user_id if "history_user_id" in locals() else None,
            org_concept_id=org_concept_id if "org_concept_id" in locals() else None,
            calculate_context_stats_fn=_calculate_context_stats,
            build_response_transformation_telemetry_payload_fn=(
                build_response_transformation_telemetry_payload
            ),
            build_turn_execution_diagnostics_fn=_build_turn_execution_diagnostics,
            finalise_llm_debug_info_fn=_finalise_llm_debug_info,
        )
        _persist_terminal_failure_turn_record_best_effort(
            local_vars=locals(),
            terminal_status="failed",
            error_text=str(e),
            error_class=type(e).__name__,
            llm_debug_info_override=(
                error_debug_info if isinstance(error_debug_info, dict) else None
            ),
            progress_snapshot=(
                error_tool_progress_snapshot
                if isinstance(error_tool_progress_snapshot, Mapping)
                else None
            ),
        )
        _finalise_generate_conversation_turn_instance(
            state=(
                conversation_turn_instance_state
                if "conversation_turn_instance_state" in locals()
                and isinstance(
                    conversation_turn_instance_state,
                    _GenerateConversationTurnInstanceState,
                )
                else _GenerateConversationTurnInstanceState()
            ),
            auxiliary_llm_calls=(
                auxiliary_llm_calls
                if "auxiliary_llm_calls" in locals()
                and isinstance(auxiliary_llm_calls, list)
                else []
            ),
            session_id=session_id if "session_id" in locals() else "",
            request_id=request_id if "request_id" in locals() else "",
            user_namespace=user_namespace if "user_namespace" in locals() else None,
            user_concept_id=user_concept_id if "user_concept_id" in locals() else None,
            org_concept_id=org_concept_id if "org_concept_id" in locals() else None,
            presenter_mode_requested=(
                presenter_mode_requested
                if "presenter_mode_requested" in locals()
                else False
            ),
            completed=False,
            final_state="generate_exception",
            debug_payload=error_debug_info,
            error=str(e),
            logger=current_app.logger,
        )
        return (
            jsonify(
                _build_generate_error_body(
                    request_id=request_id if "request_id" in locals() else None,
                    error_text=str(e),
                    error_debug_info=error_debug_info,
                    rag_trace=rag_trace if "rag_trace" in locals() else None,
                    namespace_report=(
                        namespace_report if "namespace_report" in locals() else None
                    ),
                )
            ),
            500,
        )


@von_bp.route("/history", methods=["GET"])
def history():
    """Retrieve chat history segments for the current user."""
    # SECURITY: derive user id server-side
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    requested_session_id = request.args.get("session_id")
    if isinstance(requested_session_id, str) and requested_session_id.strip():
        session_id = requested_session_id.strip()
    else:
        # Ensure session_id exists (create if needed for the current session context)
        if "session_id" not in session:
            session["session_id"] = str(uuid.uuid4())
        session_id = session["session_id"]

    if not user_concept_id:
        return jsonify(
            {
                "authenticated": False,
                "availability_status": "unauthenticated",
                "history_unavailable_reason": "not_authenticated",
                "history": [],
                "segments_returned": 0,
                "total_segments": 0,
                "has_more_history": False,
            }
        )

    requested_segments = request.args.get("segments", default=1, type=int)
    segment_count = max(1, requested_segments)
    segment_size = request.args.get("segment_size", default=None, type=int)
    if not segment_size or segment_size <= 0:
        segment_size = None
    tail_limit = request.args.get("tail_limit", default=None, type=int)
    if tail_limit is not None and tail_limit <= 0:
        tail_limit = None
    include_debug_raw = request.args.get("include_debug")
    include_debug = True
    if isinstance(include_debug_raw, str):
        parsed = include_debug_raw.strip().lower()
        if parsed in ("0", "false", "no", "n", "off"):
            include_debug = False
        elif parsed in ("1", "true", "yes", "y", "on"):
            include_debug = True
    history_tail_limit = None
    if isinstance(tail_limit, int) and tail_limit > 0:
        history_tail_limit = tail_limit
    elif isinstance(segment_size, int) and segment_size > 0:
        history_tail_limit = segment_size * max(segment_count, 1)

    try:
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        owner_user_id = user_concept_id
        shared_invite = None

        owner_user_id, shared_invite = _resolve_shared_conversation_owner(
            user_concept_id=user_concept_id, session_id=session_id
        )
        if shared_invite:
            if not owner_user_id:
                return jsonify({"error": "Not authorised for conversation"}), 403
        elif not chat_history_service.has_chat_history_session(
            user_concept_id, session_id, namespace=namespace
        ):
            return jsonify({"error": "Not authorised for conversation"}), 403

        if not isinstance(owner_user_id, str) or not owner_user_id:
            return jsonify({"error": "Not authorised for conversation"}), 403

        if shared_invite and user_concept_id and owner_user_id != user_concept_id:
            owner_history = chat_history_service.get_chat_history(
                owner_user_id, session_id
            )
            invitee_history = chat_history_service.get_chat_history(
                user_concept_id, session_id
            )
            owner_history = _apply_default_author(owner_history, owner_user_id)
            invitee_history = _apply_default_author(invitee_history, user_concept_id)
            merged_history = _merge_shared_histories(owner_history, invitee_history)

            history_truncated = False
            if history_tail_limit and len(merged_history) > history_tail_limit:
                merged_history = merged_history[-history_tail_limit:]
                history_truncated = True

            segments = chat_history_service._split_history_into_segments_with_locations(
                merged_history,
                session_id=session_id,
                include_debug=include_debug,
                owner_user_id=owner_user_id,
            )
            segments = chat_history_service._chunk_history_segments(
                segments, segment_size
            )
            meta = {"history_truncated": history_truncated}
        else:
            if shared_invite:
                owner_namespace = (
                    _derive_namespace_for_user_org(
                        owner_user_id,
                        shared_invite.get("organisation_concept_id"),
                    )
                    or namespace
                )
            else:
                # JVNAUTOSCI-1011: Use window-context namespace, not flask session
                owner_namespace = namespace
            # Fallback if namespace is None (e.g. legacy sessions without org)
            if not owner_namespace:
                owner_namespace = chat_history_service.resolve_chat_history_namespace(
                    owner_user_id
                )
            segments_result = chat_history_service.get_chat_history_segments(
                owner_user_id,
                session_id,
                include_locations=True,
                namespace=owner_namespace,
                segment_size=segment_size,
                include_debug=include_debug,
                history_tail_limit=history_tail_limit,
                return_meta=True,
            )
            if isinstance(segments_result, tuple):
                segments, meta = segments_result
            else:
                segments = segments_result
                meta = {"history_truncated": False}
        total_segments = len(segments)

        if total_segments == 0:
            return jsonify(
                {
                    "history": [],
                    "segments_returned": 0,
                    "total_segments": 0,
                    "has_more_history": False,
                }
            )

        segment_count = min(segment_count, total_segments)
        selected_segments = segments[-segment_count:]
        flattened_history = [msg for segment in selected_segments for msg in segment]
        segments_returned = len(selected_segments)
        history_truncated = bool(meta.get("history_truncated"))
        has_more = history_truncated or segments_returned < total_segments
        if history_truncated and total_segments <= segments_returned:
            total_segments = segments_returned + 1

        # Shared sessions are read from the conversation owner's history so all
        # participants see the same canonical thread.

        return jsonify(
            {
                "history": flattened_history,
                "segments_returned": segments_returned,
                "total_segments": total_segments,
                "has_more_history": has_more,
            }
        )
    except Exception as e:
        if chat_history_service.is_transient_chat_history_error(e):
            current_app.logger.warning(
                "history endpoint degraded due to transient chat history error: %s",
                e,
                exc_info=True,
            )
            return jsonify(
                _build_transient_chat_history_payload(
                    error_message="Chat history temporarily unavailable; please retry.",
                    exc=e,
                    fallback_payload={
                        "history": [],
                        "segments_returned": 0,
                        "total_segments": 0,
                        "has_more_history": False,
                    },
                )
            )
        print(f"Error retrieving history: {e}")
        return jsonify({"error": str(e)}), 500


def _build_transient_chat_history_payload(
    *,
    error_message: str,
    exc: Exception,
    fallback_payload: Dict[str, Any],
) -> Dict[str, Any]:
    payload = dict(fallback_payload)
    payload.update(
        {
            "degraded": True,
            "retryable": True,
            "availability_status": "transient_storage_error",
            "history_unavailable_reason": "transient_chat_history_error",
            "error": error_message,
            "detail": str(exc)[:300],
        }
    )
    return payload


@von_bp.route("/history/debug", methods=["GET"])
def history_debug():
    """Retrieve stored LLM debug data for a specific history entry."""
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return jsonify({"error": "Not authenticated"}), 401

    session_id = request.args.get("session_id") or session.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({"error": "session_id required"}), 400

    history_index = request.args.get("history_index", default=None, type=int)
    if history_index is None or history_index < 0:
        return jsonify({"error": "history_index required"}), 400
    view = str(request.args.get("view") or "").strip().lower()

    try:
        session_id = session_id.strip()
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace, organisation_concept_id = _resolve_history_request_scope_hints(
            user_concept_id=user_concept_id,
            effective_context=effective,
        )
        owner_user_id = user_concept_id
        shared_invite = None

        has_session = chat_history_service.has_chat_history_session(
            user_concept_id, session_id, namespace=namespace
        )
        print(
            f"[history/debug] user={user_concept_id}, session={session_id}, namespace={namespace}, has_session={has_session}, history_index={history_index}"
        )
        if not has_session:
            owner_user_id, shared_invite = _resolve_shared_conversation_owner(
                user_concept_id=user_concept_id, session_id=session_id
            )
            if not owner_user_id:
                return jsonify({"error": "Not authorised for conversation"}), 403

        owner_namespace = (
            _derive_namespace_for_user_org(
                owner_user_id,
                (
                    shared_invite.get("organisation_concept_id")
                    if shared_invite
                    else organisation_concept_id
                ),
            )
            or namespace
            or chat_history_service.resolve_chat_history_namespace(owner_user_id)
        )
        print(
            f"[history/debug] owner_user_id={owner_user_id}, owner_namespace={owner_namespace}"
        )
        debug_data = chat_history_service.get_chat_history_debug_entry(
            user_id=owner_user_id,
            session_id=session_id,
            history_index=history_index,
            namespace=owner_namespace,
        )
        # Fallback: if namespace mismatch (e.g. org session accessed without org context),
        # retry without namespace restriction since we've already verified access above.
        if not debug_data:
            print("[history/debug] Retrying without namespace restriction")
            debug_data = chat_history_service.get_chat_history_debug_entry(
                user_id=owner_user_id,
                session_id=session_id,
                history_index=history_index,
                namespace=None,
            )
        print(f"[history/debug] debug_data={bool(debug_data)}")
        if not debug_data:
            return jsonify(
                {
                    "success": False,
                    "error": "debug_not_available",
                    "history_location": {
                        "session_id": session_id,
                        "history_index": history_index,
                    },
                }
            )
        if view in {"transformations", "response_transformations"}:
            transformation_payload = None
            if isinstance(debug_data, dict):
                candidate = debug_data.get("response_transformations")
                if isinstance(candidate, dict):
                    transformation_payload = candidate
            if transformation_payload is None:
                transformation_payload = (
                    build_response_transformation_telemetry_payload(
                        request_id=(
                            debug_data.get("request_id")
                            if isinstance(debug_data, dict)
                            else None
                        )
                    )
                )
            transformations = transformation_payload.get("transformations")
            return jsonify(
                {
                    "success": True,
                    "history_location": {
                        "session_id": session_id,
                        "history_index": history_index,
                    },
                    "response_transformations": transformation_payload,
                    "transformations_count": (
                        len(transformations) if isinstance(transformations, list) else 0
                    ),
                }
            )
        return jsonify(
            {
                "success": True,
                "history_location": {
                    "session_id": session_id,
                    "history_index": history_index,
                },
                "llm_debug_data": debug_data,
            }
        )
    except Exception as e:
        print(f"Error retrieving history debug data: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/llm_call_log", methods=["GET"])
def history_llm_call_log():
    """Return a paged persisted LLM prompt/response ledger for one request."""

    from ...services.turn_execution_diagnostics_service import (
        get_turn_llm_call_log_payload,
    )

    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return jsonify({"error": "Not authenticated"}), 401

    request_id = request.args.get("request_id", type=str)
    if not isinstance(request_id, str) or not request_id.strip():
        return jsonify({"error": "request_id required"}), 400

    namespace = request.args.get("namespace", default=None, type=str)
    offset = request.args.get("offset", default=0, type=int)
    limit = request.args.get("limit", default=20, type=int)

    if offset is None or offset < 0:
        return jsonify({"error": "offset must be a non-negative integer"}), 400
    if limit is None or limit < 1 or limit > 200:
        return jsonify({"error": "limit must be between 1 and 200"}), 400

    payload = get_turn_llm_call_log_payload(
        request_id=request_id.strip(),
        namespace=(
            namespace.strip()
            if isinstance(namespace, str) and namespace.strip()
            else None
        ),
        offset=offset,
        limit=limit,
    )
    if not isinstance(payload, Mapping):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "not_found",
                    "request_id": request_id.strip(),
                }
            ),
            404,
        )

    owner_user_id = _progress_str(payload.get("derived_user_concept_id"))
    if owner_user_id and owner_user_id != user_concept_id.strip():
        session_id = _progress_str(payload.get("chat_session_id"))
        resolved_owner = None
        if session_id:
            resolved_owner, _shared_invite = _resolve_shared_conversation_owner(
                user_concept_id=user_concept_id.strip(),
                session_id=session_id,
            )
        if not resolved_owner or resolved_owner != owner_user_id:
            return jsonify({"error": "Not authorised for turn"}), 403

    payload["success"] = True
    return jsonify(payload), 200


@von_bp.route("/history/telemetry_locator", methods=["GET"])
def history_telemetry_locator():
    """Return a compact server-side locator for conversation turn telemetry."""

    from ...services.conversation_telemetry_locator_service import (
        build_conversation_llm_telemetry_locator,
    )

    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return jsonify({"error": "Not authenticated"}), 401

    session_id = request.args.get("session_id") or session.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({"error": "session_id required"}), 400

    try:
        session_id = session_id.strip()
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace, organisation_concept_id = _resolve_history_request_scope_hints(
            user_concept_id=user_concept_id,
            effective_context=effective,
        )

        owner_user_id, shared_invite = _resolve_shared_conversation_owner(
            user_concept_id=user_concept_id,
            session_id=session_id,
        )
        if not owner_user_id:
            if not chat_history_service.has_chat_history_session(
                user_concept_id,
                session_id,
                namespace=namespace,
            ) and not chat_history_service.has_chat_history_session(
                user_concept_id,
                session_id,
                namespace=None,
            ):
                return jsonify({"error": "Not authorised for conversation"}), 403
            owner_user_id = user_concept_id

        owner_namespace = (
            _derive_namespace_for_user_org(
                owner_user_id,
                (
                    shared_invite.get("organisation_concept_id")
                    if shared_invite
                    else organisation_concept_id
                ),
            )
            or namespace
        )
        if not owner_namespace:
            owner_namespace = chat_history_service.resolve_chat_history_namespace(
                owner_user_id
            )

        locator = build_conversation_llm_telemetry_locator(
            user_id=owner_user_id,
            session_id=session_id,
            namespace=owner_namespace,
            requested_user_id=user_concept_id,
            requested_namespace=namespace,
            organisation_concept_id=(
                _normalise_concept_id(shared_invite.get("organisation_concept_id"))
                if shared_invite
                else organisation_concept_id
            ),
            include_legacy=True,
        )
        locator["history_owner_user_id"] = owner_user_id
        locator["requested_user_id"] = user_concept_id
        locator["access_mode"] = (
            "invitee" if owner_user_id != user_concept_id else "owner"
        )
        return jsonify(locator)
    except Exception as e:
        if chat_history_service.is_transient_chat_history_error(e):
            current_app.logger.warning(
                "history telemetry locator degraded due to transient chat history error: %s",
                e,
                exc_info=True,
            )
            return (
                jsonify(
                    {
                        "error": "chat_history_temporarily_unavailable",
                        "detail": str(e)[:300],
                        "retryable": True,
                    }
                ),
                503,
            )
        print(f"Error retrieving history telemetry locator: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/diagnostics/export", methods=["POST"])
def export_diagnostics_snapshot():
    """Write a sanitised diagnostics snapshot to data/diagnostic_latest.json.

    This route is intentionally opt-in and user-triggered (keyboard shortcut in
    the chat UI). It keeps troubleshooting friction low for external coding
    agents while removing message bodies, prompts, and token-like fields.
    """

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "invalid_payload",
                    "message": "Expected a JSON object payload.",
                }
            ),
            400,
        )

    sanitised_payload = _sanitise_diagnostic_export_payload(payload)
    exported_at_utc = _now_utc_iso()
    file_payload = {
        "schema_version": "von_diagnostic_export.v1",
        "exported_at_utc": exported_at_utc,
        "source": "chat_shortcut",
        "sanitised": True,
        "diagnostics": sanitised_payload,
    }

    try:
        _DIAGNOSTIC_EXPORT_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DIAGNOSTIC_EXPORT_FILE_PATH.write_text(
            json.dumps(file_payload, indent=2, ensure_ascii=True, default=str) + "\n",
            encoding="utf-8",
        )
        return jsonify(
            {
                "success": True,
                "path": str(_DIAGNOSTIC_EXPORT_RELATIVE_PATH).replace("\\", "/"),
                "written_at_utc": exported_at_utc,
            }
        )
    except Exception as exc:
        current_app.logger.error(
            "Failed to write diagnostic snapshot export: %s", exc, exc_info=True
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "write_failed",
                    "message": "Could not write diagnostic export file.",
                }
            ),
            500,
        )


@von_bp.route("/history/backfill_spoken", methods=["POST"])
def history_backfill_spoken():
    """Generate and persist missing <spoken> talk track for a stored assistant turn.

    This is intended for legacy history turns where presenter channels were not
    generated or persisted at the time. The backfill:
    - requires authentication
    - does not touch session `updated_at` (so it won't reorder session recency)
    """

    from ...security.access_control import get_effective_user_concept_id

    user_concept_id = get_effective_user_concept_id()
    if not user_concept_id:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    history_location = data.get("history_location")
    if not isinstance(history_location, dict):
        history_location = {}

    target_session_id = history_location.get("session_id") or data.get("session_id")
    history_index = history_location.get("history_index")
    if history_index is None:
        history_index = data.get("history_index")

    if not isinstance(target_session_id, str) or not target_session_id.strip():
        return jsonify({"error": "session_id is required"}), 400
    if not isinstance(history_index, int) or history_index < 0:
        return jsonify({"error": "history_index must be a non-negative integer"}), 400

    force = bool(data.get("force"))

    target_session_id = target_session_id.strip()
    owner_user_id = user_concept_id
    try:
        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        if not chat_history_service.has_chat_history_session(
            user_concept_id, target_session_id, namespace=namespace
        ):
            owner_user_id, shared_invite = _resolve_shared_conversation_owner(
                user_concept_id=user_concept_id, session_id=target_session_id
            )
            if not owner_user_id:
                return jsonify({"error": "Not authorised for conversation"}), 403
    except Exception:
        owner_user_id = user_concept_id

    # Fetch the stored assistant message and associated previous user prompt.
    try:
        chat_history_coll = chat_history_service.get_chat_history_collection_service()
        if chat_history_coll is None:
            return (
                jsonify({"error": "Could not connect to chat history collection."}),
                500,
            )

        doc = chat_history_coll.find_one(
            {"user_id": owner_user_id, "session_id": target_session_id},
            {"history": 1},
        )
        history = (doc or {}).get("history") or []
        if not isinstance(history, list) or history_index >= len(history):
            return jsonify({"error": "History entry not found"}), 404

        entry = history[history_index]
        if not isinstance(entry, dict) or entry.get("role") != "assistant":
            return (
                jsonify({"error": "Target history entry is not an assistant message"}),
                400,
            )

        screen_text = entry.get("content")
        if not isinstance(screen_text, str) or not screen_text.strip():
            return jsonify({"error": "Assistant message has no content"}), 400
        screen_text = screen_text.strip()

        existing_debug = entry.get("llm_debug_data")
        existing_channels = None
        if isinstance(existing_debug, dict):
            existing_channels = existing_debug.get("presenter_channels")
        if not force and isinstance(existing_channels, dict):
            spoken_existing = existing_channels.get("spoken")
            if isinstance(spoken_existing, str) and spoken_existing.strip():
                existing_display_elements = (
                    existing_debug.get("display_elements")
                    if isinstance(existing_debug, dict)
                    else None
                )
                if not isinstance(existing_display_elements, dict):
                    existing_display_elements = build_turn_display_elements(
                        response_text=screen_text,
                        presenter_channels=existing_channels,
                    )
                existing_health = None
                if isinstance(existing_debug, dict):
                    existing_health = existing_debug.get("turn_output_health")
                if not isinstance(existing_health, dict):
                    existing_health = build_turn_output_health(
                        {
                            "presenter_channels": existing_channels,
                            "display_elements": existing_display_elements,
                        }
                    )
                return jsonify(
                    {
                        "status": "already_present",
                        "presenter_channels": existing_channels,
                        "display_elements": existing_display_elements,
                        "turn_output_health": existing_health,
                        "updated": False,
                    }
                )

        prompt_text = ""
        for i in range(history_index - 1, -1, -1):
            msg = history[i]
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "system" and msg.get("content") == "__RESET__":
                # Stop at reset boundary.
                break
            if msg.get("role") == "user":
                candidate = msg.get("content")
                if isinstance(candidate, str) and candidate.strip():
                    prompt_text = candidate.strip()
                break

    except Exception as e:
        return jsonify({"error": f"Failed reading history: {e}"}), 500

    # Load narration prompt fragments (best-effort).
    narration_prompt_text = None
    try:
        from ...services.chat_auxiliary_prompt_service import (
            get_user_specific_prompt_fragments,
        )

        narration_prompt_fragments = get_user_specific_prompt_fragments(
            user_concept_id,
            prompt_types=("#V#von_chat_narration_prompt",),
        )
        narration_texts = []
        for frag in narration_prompt_fragments:
            if not isinstance(frag, dict):
                continue
            content = frag.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            narration_texts.append(content)
        narration_prompt_text = "\n\n".join(
            text.strip() for text in narration_texts if text and text.strip()
        )
        narration_prompt_text = (
            narration_prompt_text.strip() if narration_prompt_text else None
        )
    except Exception:
        narration_prompt_text = None

    # Generate spoken talk track.
    try:
        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        org_concept_id = effective.get("organisation_id")
        llm_client = get_llm_client(
            user_concept_id=user_concept_id, org_concept_id=org_concept_id
        )
        model_name = get_active_model_name()

        def _extract_spoken_only(text: str) -> str | None:
            return _extract_tagged_block(text, "spoken")

        # Include a small client-reported timing hint for narration generation.
        # (Non-authoritative; used only for guidance.)
        timing_hint = None
        try:
            from ...services.client_capabilities_service import (
                get_client_capabilities_snapshot,
            )

            snapshot = get_client_capabilities_snapshot()
            speech = (
                snapshot.get("speech_synthesis") if isinstance(snapshot, dict) else None
            )
            speech = speech if isinstance(speech, dict) else {}
            raw_settings = speech.get("settings")
            settings = raw_settings if isinstance(raw_settings, dict) else {}

            preferred = settings.get("preferred_speaking_seconds")
            maximum = settings.get("max_speaking_seconds")

            try:
                preferred_int = int(preferred) if preferred is not None else None
            except Exception:
                preferred_int = None

            try:
                maximum_int = int(maximum) if maximum is not None else None
            except Exception:
                maximum_int = None

            if preferred_int is not None:
                preferred_int = max(1, min(preferred_int, 600))
            if maximum_int is not None:
                maximum_int = max(1, min(maximum_int, 600))

            effective_preferred = preferred_int
            if preferred_int is not None and maximum_int is not None:
                effective_preferred = min(preferred_int, maximum_int)

            if effective_preferred is not None or maximum_int is not None:
                timing_hint = (
                    "Speech timing hint (client-reported, non-authoritative): "
                    f"preferred_speaking_seconds={effective_preferred!r}, "
                    f"max_speaking_seconds={maximum_int!r}. "
                    "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                )
        except Exception:
            timing_hint = None

        narration_system = (
            "You are Von. Produce a short talk track for text-to-speech. "
            "Return ONLY one block: <spoken>...</spoken>. "
            "Do not include <screen>. Do not include code blocks. "
            "Use New Zealand English spelling."
            + ("\n\n" + timing_hint if timing_hint else "")
            + (
                "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                + narration_prompt_text
                if narration_prompt_text
                else ""
            )
        )

        narration_user = (
            "User message:\n"
            f"{prompt_text}\n\n"
            "On-screen content (do not read verbatim if long; summarise):\n"
            f"{screen_text}\n"
        )

        narration_response = _llm_generate_spoken_backfill(
            llm_client, narration_system, narration_user, model_name
        )

        spoken = _extract_spoken_only(str(narration_response))
        if not spoken:
            return jsonify({"status": "no_spoken_generated", "updated": False}), 200

        presenter_channels = {
            "screen": screen_text,
            "spoken": spoken,
            "format": "narration_fallback_v1",
        }
        spoken_backfill_reason = (
            "missing_spoken"
            if isinstance(existing_channels, dict)
            else "missing_presenter_channels"
        )
        display_elements_contract = build_turn_display_elements(
            response_text=screen_text,
            presenter_channels=presenter_channels,
            spoken_backfill_second_pass_attempted=True,
            spoken_backfill_second_pass_reason=spoken_backfill_reason,
        )
        turn_output_health = build_turn_output_health(
            {
                "presenter_channels": presenter_channels,
                "display_elements": display_elements_contract,
                "spoken_backfill_second_pass_attempted": True,
                "spoken_backfill_second_pass_reason": spoken_backfill_reason,
            }
        )

        update_result = (
            chat_history_service.upsert_presenter_channels_for_history_message(
                user_id=user_concept_id,
                session_id=target_session_id,
                history_index=history_index,
                presenter_channels=presenter_channels,
                display_elements=display_elements_contract,
                turn_output_health=turn_output_health,
                generated_at=datetime.now(timezone.utc),
                force=force,
            )
        )

        return jsonify(
            {
                "status": "ok",
                "presenter_channels": presenter_channels,
                "display_elements": display_elements_contract,
                "turn_output_health": turn_output_health,
                "updated": bool(update_result.get("updated")),
                "matched": bool(update_result.get("matched")),
            }
        )
    except Exception as e:
        return jsonify({"error": f"Failed generating spoken talk track: {e}"}), 500


@von_bp.route("/history/length", methods=["GET"])
def history_length():
    """Retrieve the chat history length for the current user."""
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not user_concept_id:
        return jsonify({"history_length": 0, "authenticated": False})

    try:
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        length = chat_history_service.get_chat_history_length(
            user_concept_id, namespace=namespace
        )
        session_count = chat_history_service.get_chat_history_session_count(
            user_concept_id, namespace=namespace
        )
        return jsonify(
            {
                "history_length": length,
                "session_count": session_count,
                "authenticated": True,
            }
        )
    except Exception as e:
        if chat_history_service.is_transient_chat_history_error(e):
            current_app.logger.warning(
                "history_length degraded due to transient chat history error: %s",
                e,
                exc_info=True,
            )
            return jsonify(
                _build_transient_chat_history_payload(
                    error_message="Chat history metrics temporarily unavailable; showing fallback values.",
                    exc=e,
                    fallback_payload={
                        "history_length": 0,
                        "session_count": 0,
                        "authenticated": True,
                    },
                )
            )
        print(f"Error retrieving history length: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/sessions", methods=["GET"])
def history_sessions():
    """Return per-session chat history counts for the current user."""

    def _resolve_authorised_active_session_id(
        *,
        user_concept_id: str,
        session_id: Any,
        namespace: str | None,
    ) -> str | None:
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        active_session_id = session_id.strip()
        if chat_history_service.has_chat_history_session(
            user_concept_id, active_session_id, namespace=namespace
        ):
            return active_session_id
        owner_user_id, shared_invite = _resolve_shared_conversation_owner(
            user_concept_id=user_concept_id, session_id=active_session_id
        )
        if owner_user_id and shared_invite:
            return active_session_id
        return None

    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")
    if not user_concept_id:
        return jsonify({"authenticated": False, "sessions": []})

    limit = request.args.get("limit", default=50, type=int)
    summary_mode = request.args.get("summary", default="full")
    if not isinstance(summary_mode, str) or not summary_mode.strip():
        summary_mode = "full"
    agent_visibility = (
        request.args.get("agent_visibility")
        or request.args.get("agent_created_visibility")
        or chat_history_service.CHAT_SESSION_AGENT_VISIBILITY_INCLUDE
    )
    keep_newest_agent_created = request.args.get("keep_newest_agent_created")
    active_session_id: str | None = None
    try:
        from ...services.shared_conversation_service import (
            list_accepted_invites_for_user,
            list_outgoing_accepted_invites_for_user,
            resolve_conversation_owner,
        )

        warnings: list[str] = []

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)

        # Fallback: extract org from namespace if not in session (JVNAUTOSCI-1004)
        # Namespace format: #V#user@org or user@org
        if (
            not organisation_concept_id
            and isinstance(namespace, str)
            and "@" in namespace
        ):
            ns_org_part = namespace.split("@", 1)[-1]
            organisation_concept_id = _normalise_concept_id(ns_org_part)
        active_session_id = _resolve_authorised_active_session_id(
            user_concept_id=user_concept_id,
            session_id=effective.get("chat_session_id"),
            namespace=namespace,
        )

        # JVNAUTOSCI-1015: Legacy conversations without namespace are excluded.
        # Run utilities/backfill_chat_history_namespace.py to migrate any old data.
        include_legacy = False
        session_result = chat_history_service.get_chat_history_session_summaries_result(
            user_concept_id,
            limit=limit,
            namespace=namespace,
            include_legacy=include_legacy,
            summary_mode=summary_mode,
            agent_visibility=agent_visibility,
            keep_newest_agent_created=keep_newest_agent_created,
        )
        sessions = (
            session_result.get("sessions") if isinstance(session_result, dict) else []
        )
        if not isinstance(sessions, list):
            sessions = []

        shared_invites: list[dict[str, Any]] = []
        try:
            raw_shared_invites = list_accepted_invites_for_user(
                user_concept_id=user_concept_id
            )
            if isinstance(raw_shared_invites, list):
                shared_invites = [
                    invite for invite in raw_shared_invites if isinstance(invite, dict)
                ]
        except Exception as exc:
            current_app.logger.warning(
                "history_sessions: failed to load accepted shared invites for %s: %s",
                user_concept_id,
                exc,
                exc_info=True,
            )
            warnings.append("accepted_invites_unavailable")

        # JVNAUTOSCI-1004: Filter shared invites by current organisation
        if organisation_concept_id:
            shared_invites = [
                invite
                for invite in shared_invites
                if (
                    _normalise_concept_id(invite.get("organisation_concept_id"))
                    in (None, organisation_concept_id)
                )
            ]

        invite_by_session: dict[str, dict] = {}
        for invite in shared_invites:
            invite_session_id = invite.get("session_id")
            if isinstance(invite_session_id, str) and invite_session_id:
                invite_by_session.setdefault(invite_session_id, invite)

        if invite_by_session:
            session_enrichment_errors = 0
            for summary in sessions:
                if not isinstance(summary, dict):
                    continue
                session_id = summary.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    continue
                invite = invite_by_session.get(session_id)
                if not isinstance(invite, dict):
                    continue
                try:
                    inviter_id = _normalise_concept_id(invite.get("inviter_user_id"))
                    owner_id = _normalise_concept_id(
                        invite.get("conversation_owner_user_id")
                        or invite.get("inviter_user_id")
                    )
                    if not invite.get("conversation_owner_user_id"):
                        resolved_owner = resolve_conversation_owner(
                            session_id=session_id
                        )
                        resolved_owner_id = _normalise_concept_id(resolved_owner)
                        if resolved_owner_id:
                            owner_id = resolved_owner_id

                    summary["shared_with_me"] = True
                    if inviter_id:
                        summary["shared_from_user_id"] = inviter_id
                    if owner_id:
                        summary["shared_owner_user_id"] = owner_id
                    if invite.get("invite_id"):
                        summary["invite_id"] = invite.get("invite_id")
                    shared_timestamp = (
                        invite.get("accepted_at")
                        or invite.get("updated_at")
                        or invite.get("created_at")
                    )
                    summary["shared_accepted_at"] = shared_timestamp
                except Exception:
                    session_enrichment_errors += 1
                    current_app.logger.warning(
                        "history_sessions: failed to enrich shared session row %s",
                        session_id,
                        exc_info=True,
                    )
            if session_enrichment_errors > 0:
                warnings.append(
                    f"shared_session_enrichment_errors:{session_enrichment_errors}"
                )

        # Flag owner's sessions that have accepted participants (JVNAUTOSCI-1002)
        # This enables the owner to subscribe to SSE updates from participants
        outgoing_accepted: list[dict[str, Any]] = []
        try:
            raw_outgoing = list_outgoing_accepted_invites_for_user(
                user_concept_id=user_concept_id
            )
            if isinstance(raw_outgoing, list):
                outgoing_accepted = [
                    invite for invite in raw_outgoing if isinstance(invite, dict)
                ]
        except Exception as exc:
            current_app.logger.warning(
                "history_sessions: failed to load outgoing accepted invites for %s: %s",
                user_concept_id,
                exc,
                exc_info=True,
            )
            warnings.append("outgoing_invites_unavailable")

        if organisation_concept_id:
            outgoing_accepted = [
                invite
                for invite in outgoing_accepted
                if (
                    _normalise_concept_id(invite.get("organisation_concept_id"))
                    in (None, organisation_concept_id)
                )
            ]
        outgoing_by_session: dict[str, list] = {}
        for invite in outgoing_accepted:
            out_session_id = invite.get("session_id")
            if isinstance(out_session_id, str) and out_session_id:
                outgoing_by_session.setdefault(out_session_id, []).append(invite)
        if outgoing_by_session:
            for summary in sessions:
                if not isinstance(summary, dict):
                    continue
                session_id = summary.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    continue
                if session_id in outgoing_by_session:
                    summary["has_shared_participants"] = True
        existing_session_ids = {
            s.get("session_id") for s in sessions if isinstance(s, dict)
        }
        shared_sessions: list[dict[str, Any]] = []
        shared_invite_resolution_errors = 0
        for invite in shared_invites:
            try:
                inviter_id = _normalise_concept_id(invite.get("inviter_user_id"))
                owner_id = _normalise_concept_id(
                    invite.get("conversation_owner_user_id")
                    or invite.get("inviter_user_id")
                )
                session_id = invite.get("session_id")
                if not owner_id or not isinstance(session_id, str) or not session_id:
                    continue
                if not invite.get("conversation_owner_user_id"):
                    resolved_owner = resolve_conversation_owner(session_id=session_id)
                    resolved_owner_id = _normalise_concept_id(resolved_owner)
                    if resolved_owner_id:
                        owner_id = resolved_owner_id
                if session_id in existing_session_ids:
                    continue
                owner_namespace = _derive_namespace_for_user_org(
                    owner_id, invite.get("organisation_concept_id")
                ) or chat_history_service.resolve_chat_history_namespace(owner_id)
                summary = chat_history_service.get_chat_history_session_summary(
                    owner_id,
                    session_id,
                    namespace=owner_namespace,
                    summary_mode=summary_mode,
                )
                if not isinstance(summary, dict) and owner_id:
                    summary = chat_history_service.get_chat_history_session_summary(
                        owner_id,
                        session_id,
                        namespace=None,
                        summary_mode=summary_mode,
                    )
                if not isinstance(summary, dict):
                    continue
                shared_timestamp = (
                    invite.get("accepted_at")
                    or invite.get("updated_at")
                    or invite.get("created_at")
                )
                existing_last = summary.get("last_message_at")
                if not existing_last:
                    shared_dt = chat_history_service._coerce_datetime(shared_timestamp)
                    if shared_dt:
                        summary["last_message_at"] = shared_dt.isoformat().replace(
                            "+00:00", "Z"
                        )
                summary["shared_with_me"] = True
                if inviter_id:
                    summary["shared_from_user_id"] = inviter_id
                summary["shared_owner_user_id"] = owner_id
                summary["invite_id"] = invite.get("invite_id")
                summary["shared_accepted_at"] = shared_timestamp
                shared_sessions.append(summary)
            except Exception:
                shared_invite_resolution_errors += 1
                current_app.logger.warning(
                    "history_sessions: failed to resolve shared invite row",
                    exc_info=True,
                )
        if shared_invite_resolution_errors > 0:
            warnings.append(
                f"shared_invite_resolution_errors:{shared_invite_resolution_errors}"
            )

        combined = [
            session_row
            for session_row in (sessions + shared_sessions)
            if isinstance(session_row, dict)
        ]
        combined.sort(
            key=lambda s: (
                chat_history_service._coerce_datetime(
                    s.get("last_message_at") if isinstance(s, dict) else None
                )
                or datetime(1970, 1, 1, tzinfo=timezone.utc)
            ),
            reverse=True,
        )

        response_payload: dict[str, Any] = {
            "authenticated": True,
            "sessions": combined,
            "active_session_id": active_session_id,
        }
        if isinstance(session_result, dict):
            response_payload.update(
                {
                    "agent_visibility": session_result.get("agent_visibility"),
                    "agent_visibility_applied": session_result.get(
                        "agent_visibility_applied"
                    )
                    is True,
                    "keep_newest_agent_created": session_result.get(
                        "keep_newest_agent_created"
                    )
                    is True,
                    "agent_created_session_total": session_result.get(
                        "agent_created_session_total",
                        0,
                    ),
                    "hidden_agent_created_session_count": session_result.get(
                        "hidden_agent_created_session_count",
                        0,
                    ),
                    "newest_visible_agent_created_session_id": session_result.get(
                        "newest_visible_agent_created_session_id"
                    ),
                    "total_after_agent_visibility": session_result.get(
                        "total_after_agent_visibility",
                        len(sessions),
                    ),
                    "hidden_by_limit_count": session_result.get(
                        "hidden_by_limit_count",
                        0,
                    ),
                    "raw_session_count": session_result.get("raw_session_count"),
                    "limit": session_result.get("limit", limit),
                }
            )
        if warnings:
            response_payload["warnings"] = warnings

        return jsonify(response_payload)
    except Exception as e:
        if chat_history_service.is_transient_chat_history_error(e):
            current_app.logger.warning(
                "history_sessions degraded due to transient chat history error: %s",
                e,
                exc_info=True,
            )
            return jsonify(
                _build_transient_chat_history_payload(
                    error_message="Conversation list temporarily unavailable; showing fallback state.",
                    exc=e,
                    fallback_payload={
                        "authenticated": True,
                        "sessions": [],
                        "active_session_id": active_session_id,
                        "warnings": ["chat_history_temporarily_unavailable"],
                    },
                )
            )
        current_app.logger.exception("Error retrieving history sessions")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/models", methods=["GET"])
def get_models():
    """API endpoint to fetch the list of available models."""
    # Assuming list_models_func is stored in app config or accessible globally
    list_models_func = current_app.config.get("LIST_MODELS_FUNC")
    if not list_models_func:
        return jsonify({"error": "Model listing function not configured."}), 500
    try:
        models = list_models_func()
        return jsonify(models), 200
    except Exception as e:
        print(f"Error fetching models: {e}")  # Log error server-side
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/search", methods=["GET"])
def search_concepts_endpoint():
    """API endpoint to search for concepts by name.

    Query parameters:
    - q: Search query string (required)
    - limit: Maximum results to return (default: 8)
    """
    try:
        from ...services.concept_search_service import search_concepts

        query = request.args.get("q", "").strip()
        limit = request.args.get("limit", default=8, type=int)

        if not query:
            return jsonify({"results": [], "total_count": 0}), 200

        result = search_concepts(
            query=query, match_type="substring", limit=limit, include_description=False
        )

        # Format results for autocomplete
        formatted_results = [
            {
                "id": item.get("concept_id"),  # Frontend expects 'id' field
                "concept_id": item.get("concept_id"),
                "name": item.get("name") or item.get("concept_id"),
                "kind": item.get("kind", "unknown"),
            }
            for item in result.get("results", [])
        ]

        return (
            jsonify(
                {
                    "results": formatted_results,
                    "total_count": result.get("total_count", 0),
                }
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(f"Concept search error: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/render_markdown", methods=["POST"])
def render_markdown_endpoint():
    """Render markdown to sanitised HTML.

    Request JSON:
    - text: markdown string

    Response JSON:
    - html: sanitised HTML string
    """

    try:
        from ...services.markdown_render_service import render_markdown_to_safe_html

        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        if not isinstance(text, str):
            return jsonify({"error": "Field 'text' must be a string"}), 400

        if len(text) > 200_000:
            return jsonify({"error": "Markdown payload too large"}), 413

        html = render_markdown_to_safe_html(text)
        return jsonify({"html": html}), 200
    except Exception as e:
        current_app.logger.error(f"render_markdown error: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/reset", methods=["POST"])
def reset_context():
    """Reset the conversation context."""
    try:
        user_concept_id = session.get("user_concept_id")
        session_id = session.get("session_id")

        if user_concept_id and session_id:
            chat_history_service.add_reset_marker_to_history(
                user_concept_id, session_id
            )

        # Clear the conversation context
        current_app.config["CONTEXT"] = []
        _safe_app_log("info", "Context reset successfully")
        return (
            jsonify({"status": "reset", "message": "Context reset successfully"}),
            200,
        )
    except Exception as e:
        _safe_app_log("exception", "Error resetting context: %s", e)
        return jsonify({"error": f"Failed to reset context: {str(e)}"}), 500


# Phase 2: Organisation and Role Selection Endpoints
@von_bp.route("/api/session/set_user_concept", methods=["POST"])
def set_user_concept():
    """Set the current user concept in the session.

    This aligns the authenticated session identity with the user selected in Settings.

    Request body: {user_concept_id: str}
    Returns: {user_id, organisation_id, role, namespace, status: 'updated'}
    """
    try:
        from ...services.namespace_service import derive_namespace
        from ...security.access_control import get_effective_user_concept_id

        authenticated_id = get_effective_user_concept_id() or (
            session.get("user_id")
            or session.get("user_concept_id")
            or session.get("user_email")
        )
        if not authenticated_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        user_concept_id = data.get("user_concept_id")
        if not isinstance(user_concept_id, str) or not user_concept_id.strip():
            return jsonify({"error": "user_concept_id required"}), 400

        user_concept_id = user_concept_id.strip()
        if not user_concept_id.startswith("#V#"):
            user_concept_id = f"#V#{user_concept_id}"

        user_slug = user_concept_id[3:]
        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        existing_user_concept_id = session.get("user_concept_id")
        if isinstance(existing_user_concept_id, str):
            existing_user_concept_id = existing_user_concept_id.strip()
            if existing_user_concept_id and not existing_user_concept_id.startswith(
                "#V#"
            ):
                existing_user_concept_id = f"#V#{existing_user_concept_id}"
        else:
            existing_user_concept_id = None

        window_session_id = request.headers.get("X-Von-Window-Session")
        effective_context = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = effective_context.get(
            "organisation_id"
        ) or session.get("organisation_concept_id")
        role_in_org = effective_context.get("role") or session.get("role_in_org")
        org_slug = None
        if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
            org_slug_raw = organisation_concept_id.strip()
            if org_slug_raw.startswith("#V#"):
                org_slug_raw = org_slug_raw[3:]
            if "@" in org_slug_raw:
                org_slug_raw = org_slug_raw.split("@", 1)[0]
            if "+" in org_slug_raw:
                org_slug_raw = org_slug_raw.split("+", 1)[0]
            org_slug = re.sub(r"[^a-z0-9]+", "_", org_slug_raw.strip().lower()).strip(
                "_"
            )

        namespace = derive_namespace(user_slug, org_slug, role_in_org)

        # Store the concept id for authoritative identity.
        session["user_concept_id"] = user_concept_id
        # Keep backward compatibility with code that still reads session['user_id'].
        session["user_id"] = user_concept_id

        # Clear org-scoped context only when switching between different user concepts.
        # If we are simply backfilling user_concept_id for an already-authenticated session,
        # keep any existing org selection and recompute namespace accordingly.
        if existing_user_concept_id and existing_user_concept_id != user_concept_id:
            organisation_concept_id = None
            role_in_org = None
            session.pop("organisation_concept_id", None)
            session.pop("role_in_org", None)
            if window_session_id:
                clear_window_organisation(window_session_id, namespace, user_concept_id)
        elif org_slug:
            session["organisation_concept_id"] = org_slug
            if role_in_org:
                session["role_in_org"] = role_in_org
            else:
                session.pop("role_in_org", None)
        session["namespace"] = namespace
        session.modified = True

        organisation_id_response = None
        if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
            organisation_id_response = organisation_concept_id.strip()
            if not organisation_id_response.startswith("#V#"):
                organisation_id_response = f"#V#{organisation_id_response}"

        return (
            jsonify(
                {
                    "status": (
                        "updated"
                        if existing_user_concept_id != user_concept_id
                        else "unchanged"
                    ),
                    "user_id": user_concept_id,
                    "organisation_id": organisation_id_response,
                    "role": role_in_org,
                    "namespace": namespace,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error setting user concept: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/set_organisation", methods=["POST"])
def set_organisation():
    """
    Set the current organisation context in the session.

    Request body: {organisation_concept_id: str} or empty dict to clear
    - organisation_concept_id can be a concept ID with or without #V# prefix
    - If organisation_concept_id is present but empty/null, treat as clear request

    Supports window-scoped sessions via X-Von-Window-Session header (JVNAUTOSCI-1011).
    If the header is present, org context is stored in per-window memory store
    instead of the browser-wide Flask session.

    Returns: {user_id, organisation_id, role, namespace, status: 'updated'}
    """
    try:
        from ...services.namespace_service import derive_namespace
        from ...security.role_resolver import get_user_role

        user_id = (
            session.get("user_concept_id")
            or session.get("user_id")
            or session.get("user_email")
        )
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401

        # JVNAUTOSCI-1011: Check for window session header
        window_session_id = request.headers.get("X-Von-Window-Session")

        data = request.get_json() or {}
        org_id = data.get("organisation_concept_id")

        # Normalise user id to a safe slug for namespace/role lookup
        user_slug = str(user_id)
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@", 1)[0]
        if "+" in user_slug:
            user_slug = user_slug.split("+", 1)[0]
        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        # Check if this is a clear request (empty dict or explicit null/empty string)
        is_clear_request = "organisation_concept_id" in data and not org_id

        # Handle clearing (personal context / no org)
        if is_clear_request:
            namespace = derive_namespace(user_slug)

            # JVNAUTOSCI-1011: Use window session if header present
            if window_session_id:
                clear_window_organisation(window_session_id, namespace, user_id)
            else:
                # Fallback: update Flask session
                session.pop("organisation_concept_id", None)
                session.pop("role_in_org", None)
                # JVNAUTOSCI-1004: Clear chat session_id when org changes
                session.pop("session_id", None)
                session["namespace"] = namespace
                session.modified = True

            return (
                jsonify(
                    {
                        "status": "updated",
                        "user_id": user_id,
                        "organisation_id": None,
                        "role": None,
                        "namespace": namespace,
                        "window_session_id": window_session_id,
                    }
                ),
                200,
            )

        # If org_id not provided and not an explicit clear, that's an error
        if not org_id:
            return jsonify({"error": "organisation_concept_id required"}), 400

        # TODO: Validate user is member of org (once membership model exists)
        # For now, allow any org switch

        # org_id may arrive as a concept id (e.g., "#V#university_of_auckland_strong_ai_lab")
        org_slug = str(org_id)
        if org_slug.startswith("#V#"):
            org_slug = org_slug[3:]
        org_slug = org_slug.strip().lower().replace(" ", "_")

        # Get role for this user in this org (stub resolver expects slugs)
        try:
            role_in_org = get_user_role(user_slug, org_slug)
        except Exception:
            role_in_org = "member"  # Default fallback

        # Derive composite namespace using slug values
        namespace = derive_namespace(user_slug, org_slug)

        # JVNAUTOSCI-1011: Use window session if header present
        if window_session_id:
            set_window_organisation(
                window_session_id=window_session_id,
                organisation_concept_id=org_slug,
                role_in_org=role_in_org,
                namespace=namespace,
                user_id=user_id,
            )
        else:
            # Fallback: update Flask session (for clients without window session support)
            session["organisation_concept_id"] = org_slug
            session["role_in_org"] = role_in_org
            session["namespace"] = namespace
            # JVNAUTOSCI-1004: Clear chat session_id when org changes to avoid
            # showing conversation from previous org context
            session.pop("session_id", None)
            session.modified = True

        # Return concept ID form in API response (with #V# prefix)
        concept_id_response = (
            f"#V#{org_slug}" if not str(org_id).startswith("#V#") else org_id
        )

        return (
            jsonify(
                {
                    "status": "updated",
                    "user_id": user_id,
                    "organisation_id": concept_id_response,
                    "role": role_in_org,
                    "namespace": namespace,
                    "window_session_id": window_session_id,
                }
            ),
            200,
        )

    except Exception as e:
        print(f"Error setting organisation: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/context", methods=["GET"])
def get_session_context():
    """
    Get current session context (user, organisation, role, namespace).

    Supports window-scoped sessions via X-Von-Window-Session header (JVNAUTOSCI-1011).
    If the header is present, org context is read from per-window memory store
    instead of the browser-wide Flask session, enabling different org contexts
    in different browser windows.

    Returns: {user_id, organisation_id, role, namespace, authenticated, window_session_id?}
    """
    try:
        from ...services.namespace_service import derive_namespace
        from ...security.role_resolver import get_user_role

        user_id = (
            session.get("user_concept_id")
            or session.get("user_id")
            or session.get("user_email")
        )
        if not user_id:
            return (
                jsonify(
                    {
                        "authenticated": False,
                        "user_id": None,
                        "organisation_id": None,
                        "role": None,
                        "namespace": None,
                    }
                ),
                200,
            )

        # JVNAUTOSCI-1011: Check for window session header
        window_session_id = request.headers.get("X-Von-Window-Session")

        # Get effective context from window session or Flask session
        effective = get_effective_context(window_session_id, dict(session), user_id)

        org_id = effective.get("organisation_id")
        role_in_org = effective.get("role")
        namespace = effective.get("namespace")

        # If no namespace resolved, derive it
        if not namespace:
            user_slug = str(user_id)
            if user_slug.startswith("#V#"):
                user_slug = user_slug[3:]
            if "@" in user_slug:
                user_slug = user_slug.split("@", 1)[0]
            if "+" in user_slug:
                user_slug = user_slug.split("+", 1)[0]
            user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")
            if org_id:
                # Get role if not in session
                if not role_in_org:
                    try:
                        role_in_org = get_user_role(user_slug, org_id)
                    except Exception:
                        role_in_org = "member"
                namespace = derive_namespace(user_slug, org_id)
            else:
                namespace = derive_namespace(user_slug)

        organisation_id_response = None
        if isinstance(org_id, str) and org_id.strip():
            organisation_id_response = org_id.strip()
            if not organisation_id_response.startswith("#V#"):
                organisation_id_response = f"#V#{organisation_id_response}"

        response_data = {
            "authenticated": True,
            "user_id": user_id,
            "organisation_id": organisation_id_response,
            "role": role_in_org,
            "namespace": namespace,
        }

        # Include window_session_id in response so frontend can confirm which session is active
        if window_session_id:
            response_data["window_session_id"] = window_session_id
            response_data["context_source"] = effective.get("source", "window_session")

        return jsonify(response_data), 200

    except Exception as e:
        print(f"Error getting session context: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/set_chat_session", methods=["POST"])
def set_chat_session():
    """Set the active chat session_id for the current authenticated user.

    Request body: {session_id: str, include_history?: bool}
    Returns: {status, session_id, session_name, history}

    This enables the frontend to switch to a prior session and continue it.
    """
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()
        include_history = data.get("include_history", True)
        if isinstance(include_history, bool):
            pass
        elif isinstance(include_history, str):
            parsed = include_history.strip().lower()
            if parsed in ("0", "false", "no", "n", "off"):
                include_history = False
            elif parsed in ("1", "true", "yes", "y", "on"):
                include_history = True
            else:
                include_history = True
        elif isinstance(include_history, (int, float)):
            include_history = include_history != 0
        else:
            include_history = True

        # Verify the session belongs to this user.
        coll = chat_history_service.get_chat_history_collection_service(read_only=True)
        if coll is None:
            return jsonify({"error": "Chat history unavailable"}), 503

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        query = chat_history_service.build_chat_history_query(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=namespace,
        )
        projection = {"session_name": 1}
        if include_history:
            projection["history"] = 1
        user_doc = chat_history_service.find_chat_history_document_for_read(
            coll, query, projection
        )
        owner_user_id, invite = _resolve_shared_conversation_owner(
            user_concept_id=user_concept_id, session_id=session_id
        )
        owner_doc = None
        owner_namespace = None
        if owner_user_id and invite:
            org_id = invite.get("organisation_concept_id") if invite else None
            owner_namespace = _derive_namespace_for_user_org(
                owner_user_id, org_id
            ) or chat_history_service.resolve_chat_history_namespace(owner_user_id)
            shared_query = chat_history_service.build_chat_history_query(
                user_id=owner_user_id,
                session_id=session_id,
                namespace=owner_namespace,
            )
            owner_doc = chat_history_service.find_chat_history_document_for_read(
                coll, shared_query, projection
            )
            if not owner_doc:
                shared_query = chat_history_service.build_chat_history_query(
                    user_id=owner_user_id,
                    session_id=session_id,
                    namespace=None,
                )
                owner_doc = chat_history_service.find_chat_history_document_for_read(
                    coll, shared_query, projection
                )

        doc = owner_doc or user_doc
        if not doc:
            return jsonify({"error": "Session not found"}), 404

        session_name = doc.get("session_name")

        def _normalise_timestamp(value):
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.isoformat().replace("+00:00", "Z")
            return value

        normalised_history = []
        if include_history:
            history = doc.get("history") or []
            if not isinstance(history, list):
                history = []
            if invite and owner_doc is not None:
                owner_history = owner_doc.get("history") or []
                if not isinstance(owner_history, list):
                    owner_history = []
                user_history = user_doc.get("history") if user_doc else []
                if not isinstance(user_history, list):
                    user_history = []
                owner_history = _apply_default_author(owner_history, owner_user_id)
                user_history = _apply_default_author(user_history, user_concept_id)
                history = _merge_shared_histories(owner_history, user_history)
                if owner_user_id and len(history) > len(owner_history):
                    try:
                        chat_history_service.set_chat_history_for_session(
                            user_id=owner_user_id,
                            session_id=session_id,
                            history=history,
                            namespace=owner_namespace,
                            set_updated_at=False,
                            extra_set_fields={
                                "shared_history_migrated_at": datetime.now(
                                    timezone.utc
                                ),
                                "shared_history_migrated_from": user_concept_id,
                            },
                        )
                    except Exception:
                        pass

            for msg in history:
                if not isinstance(msg, dict):
                    continue
                out = dict(msg)
                if "timestamp" in out:
                    out["timestamp"] = _normalise_timestamp(out.get("timestamp"))
                normalised_history.append(out)

        # Switch active session (window-scoped if window session is present).
        # JVNAUTOSCI-1011: Store in window session context to avoid cross-window leaks.
        if window_session_id:
            from ...services.window_session_context_service import (
                set_window_chat_session,
            )

            set_window_chat_session(window_session_id, session_id, user_concept_id)
        else:
            # Fallback for legacy clients without window session support
            session["session_id"] = session_id
            session.modified = True

        # Clear any non-persistent context cache.
        try:
            current_app.config["CONTEXT"] = []
        except Exception:
            pass

        return (
            jsonify(
                {
                    "status": "updated",
                    "session_id": session_id,
                    "session_name": session_name,
                    "history": normalised_history,
                }
            ),
            200,
        )
    except Exception as e:
        if chat_history_service.is_transient_chat_history_error(e):
            current_app.logger.warning(
                "set_chat_session transient failure for session switch: %s",
                e,
                exc_info=True,
            )
            return (
                jsonify(
                    {
                        "error": "Conversation switch temporarily unavailable; please retry.",
                        "detail": str(e)[:300],
                        "retryable": True,
                        "degraded": True,
                    }
                ),
                503,
            )
        print(f"Error setting chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/assign_chat_session_org", methods=["POST"])
def assign_chat_session_org():
    """Assign a chat session to the current organisation namespace if missing."""
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        if not organisation_concept_id:
            return jsonify({"error": "No organisation context"}), 400

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        coll = chat_history_service.get_chat_history_collection_service()
        if coll is None:
            return jsonify({"error": "Chat history unavailable"}), 503

        target_namespace = _derive_namespace_for_user_org(
            user_concept_id, organisation_concept_id
        )
        if not target_namespace:
            return jsonify({"error": "Unable to derive namespace"}), 500

        query = chat_history_service.build_chat_history_query(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=None,
            include_legacy=True,
        )
        doc = coll.find_one(query, {"namespace": 1, "organisation_concept_id": 1})
        if not isinstance(doc, dict):
            return jsonify({"error": "Session not found"}), 404

        existing_namespace = doc.get("namespace")
        if isinstance(existing_namespace, str) and existing_namespace.strip():
            if existing_namespace.strip() != target_namespace:
                return (
                    jsonify(
                        {
                            "error": "Session already assigned to a different organisation",
                            "namespace": existing_namespace,
                        }
                    ),
                    409,
                )
            return jsonify({"status": "ok", "namespace": existing_namespace}), 200

        existing_org = _normalise_concept_id(doc.get("organisation_concept_id"))
        if existing_org and existing_org != organisation_concept_id:
            return (
                jsonify(
                    {
                        "error": "Session already assigned to a different organisation",
                        "organisation_concept_id": existing_org,
                    }
                ),
                409,
            )

        result = coll.update_one(
            {"_id": doc.get("_id")},
            {
                "$set": {
                    "namespace": target_namespace,
                    "organisation_concept_id": organisation_concept_id,
                    "role_in_org": session.get("role_in_org"),
                }
            },
        )

        return (
            jsonify(
                {
                    "status": "updated",
                    "session_id": session_id,
                    "namespace": target_namespace,
                    "organisation_concept_id": organisation_concept_id,
                    "matched": bool(getattr(result, "matched_count", 0) > 0),
                    "updated": bool(getattr(result, "modified_count", 0) > 0),
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error assigning chat session org: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/move_chat_session_org", methods=["POST"])
def move_chat_session_org():
    """Move a conversation the user owns to a different organisation.

    JVNAUTOSCI-1039: Allows users who are members of multiple organisations
    to move a conversation from one organisation context to another.

    Request body:
        session_id: The conversation to move
        target_organisation_id: The organisation to move to

    Security:
        - User must be authenticated
        - User must own the conversation
        - User must be a member of both the current and target organisations
        - Shared invites for users not in target org are revoked
    """
    try:
        from ...services.organisation_membership_service import (
            get_organisation_members,
            get_user_memberships,
        )
        from ...services.shared_conversation_service import (
            list_active_invites_for_session,
            revoke_invites_for_session,
        )
        from ...services.episode_logging_service import log_episode

        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        target_org_id = _normalise_concept_id(
            data.get("target_organisation_id") or data.get("organisation_id")
        )

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        if not target_org_id:
            return jsonify({"error": "target_organisation_id required"}), 400

        # Verify user is a member of the target organisation
        memberships = get_user_memberships(user_concept_id)
        user_org_ids = {
            m.get("organisation_concept_id")
            for m in memberships.get("memberships", [])
            if m.get("organisation_concept_id")
        }
        if target_org_id not in user_org_ids:
            return (
                jsonify({"error": "Not a member of target organisation"}),
                403,
            )

        coll = chat_history_service.get_chat_history_collection_service()
        if coll is None:
            return jsonify({"error": "Chat history unavailable"}), 503

        # Find the conversation - must belong to this user
        query = chat_history_service.build_chat_history_query(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=None,
            include_legacy=True,
        )
        doc = coll.find_one(
            query,
            {
                "namespace": 1,
                "organisation_concept_id": 1,
                "user_id": 1,
                "session_id": 1,
            },
        )
        if not isinstance(doc, dict):
            return jsonify({"error": "Conversation not found"}), 404

        # Verify ownership
        doc_user_id = _normalise_concept_id(doc.get("user_id"))
        if doc_user_id != user_concept_id:
            return jsonify({"error": "Not the owner of this conversation"}), 403

        # Check current organisation
        current_org_id = _normalise_concept_id(doc.get("organisation_concept_id"))
        current_namespace = doc.get("namespace")

        if current_org_id == target_org_id:
            return (
                jsonify(
                    {
                        "status": "ok",
                        "message": "Conversation already in target organisation",
                        "namespace": current_namespace,
                    }
                ),
                200,
            )

        # Derive new namespace for target organisation
        target_namespace = _derive_namespace_for_user_org(
            user_concept_id, target_org_id
        )
        if not target_namespace:
            return jsonify({"error": "Unable to derive namespace for target org"}), 500

        # Handle shared conversation invites
        # Get members of the target org to determine which invites to keep
        target_org_members = get_organisation_members(target_org_id)
        target_member_ids = {
            _normalise_concept_id(m.get("user_concept_id"))
            for m in target_org_members.get("members", [])
            if m.get("user_concept_id")
        }

        # Check existing invites
        active_invites = list_active_invites_for_session(session_id=session_id)
        invites_to_revoke = [
            inv
            for inv in active_invites
            if _normalise_concept_id(inv.get("invitee_user_id"))
            not in target_member_ids
        ]

        revoke_result = {"revoked_count": 0, "invitee_ids": []}
        if invites_to_revoke:
            # Filter out None values for type safety
            valid_member_ids = [m for m in target_member_ids if m is not None]
            revoke_result = revoke_invites_for_session(
                session_id=session_id,
                exclude_user_ids=valid_member_ids,
                reason=f"conversation_moved_to_{target_org_id}",
            )

        # Update the conversation
        update_fields = {
            "namespace": target_namespace,
            "organisation_concept_id": target_org_id,
            "previous_namespace": current_namespace,
            "previous_organisation_concept_id": current_org_id,
            "moved_at": datetime.now(timezone.utc),
            "moved_by": user_concept_id,
        }

        # Clear RAG indexed status so conversation will be re-indexed for new namespace
        unset_fields = {
            "rag_indexed_success": "",
            "rag_indexed_failed": "",
            "rag_indexed_at": "",
        }

        result = coll.update_one(
            {"_id": doc.get("_id")},
            {"$set": update_fields, "$unset": unset_fields},
        )

        # Log the move operation
        log_episode(
            episode_type="conversation_moved",
            actor_user_id=user_concept_id,
            organisation_concept_id=target_org_id,
            session_id=session_id,
            payload={
                "from_organisation": current_org_id,
                "to_organisation": target_org_id,
                "from_namespace": current_namespace,
                "to_namespace": target_namespace,
                "invites_revoked": revoke_result.get("revoked_count", 0),
                "revoked_invitee_ids": revoke_result.get("invitee_ids", []),
            },
            status="moved",
        )

        return (
            jsonify(
                {
                    "status": "moved",
                    "session_id": session_id,
                    "namespace": target_namespace,
                    "organisation_concept_id": target_org_id,
                    "previous_namespace": current_namespace,
                    "previous_organisation_concept_id": current_org_id,
                    "matched": bool(getattr(result, "matched_count", 0) > 0),
                    "updated": bool(getattr(result, "modified_count", 0) > 0),
                    "invites_revoked": revoke_result.get("revoked_count", 0),
                    "revoked_invitee_ids": revoke_result.get("invitee_ids", []),
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error moving chat session org: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/create_chat_session", methods=["POST"])
def create_chat_session():
    """Create and switch to a new named chat session for the current user."""
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_name = (
            data.get("session_name") or data.get("name") or data.get("chat_name")
        )

        session_id = str(uuid.uuid4())

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )

        result = chat_history_service.create_chat_session(
            user_id=user_concept_id,
            session_id=session_id,
            session_name=session_name,
            namespace=effective.get("namespace"),
            organisation_concept_id=effective.get("organisation_id"),
            role_in_org=effective.get("role"),
            **_normalise_create_chat_session_provenance(data),
        )

        _set_active_chat_session_for_request(
            session_id=session_id,
            user_concept_id=user_concept_id,
            window_session_id=window_session_id,
        )

        try:
            current_app.config["CONTEXT"] = []
        except Exception:
            pass

        return (
            jsonify(
                {
                    "status": "created",
                    "session_id": session_id,
                    "session_name": result.get("session_name"),
                    "history": [],
                    "origin_kind": result.get("origin_kind"),
                    "created_by_actor_concept_id": result.get(
                        "created_by_actor_concept_id"
                    ),
                    "created_by_actor_type": result.get("created_by_actor_type"),
                    "is_agent_created": result.get("is_agent_created") is True,
                    "test_artifact_kind": result.get("test_artifact_kind"),
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error creating chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/rename_chat_session", methods=["POST"])
def rename_chat_session():
    """Rename an existing chat session for the current user."""
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()
        include_history = data.get("include_history", True)
        if isinstance(include_history, bool):
            pass
        elif isinstance(include_history, str):
            parsed = include_history.strip().lower()
            if parsed in ("0", "false", "no", "n", "off"):
                include_history = False
            elif parsed in ("1", "true", "yes", "y", "on"):
                include_history = True
            else:
                include_history = True
        elif isinstance(include_history, (int, float)):
            include_history = include_history != 0
        else:
            include_history = True

        session_name = data.get("session_name") or data.get("name")
        if not isinstance(session_name, str) or not session_name.strip():
            return jsonify({"error": "session_name required"}), 400

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        result = chat_history_service.rename_chat_session(
            user_id=user_concept_id,
            session_id=session_id,
            session_name=session_name,
            namespace=namespace,
        )

        if not result.get("matched"):
            return jsonify({"error": "Session not found"}), 404

        return (
            jsonify(
                {
                    "status": "updated",
                    "session_id": session_id,
                    "session_name": result.get("session_name"),
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error renaming chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/delete_chat_session", methods=["POST"])
def delete_chat_session():
    """Delete a chat session for the current user.

    JVNAUTOSCI-1014: Only allows deletion of sessions with fewer than a threshold
    number of turns (currently 4) to prevent accidental deletion of substantial
    conversations.
    """
    MAX_DELETABLE_TURNS = 4
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)

        # Verify session exists and check message count
        session_doc = chat_history_service.get_chat_history_session_summary(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=namespace,
            summary_mode="light",
        )
        if not session_doc:
            return jsonify({"error": "Session not found"}), 404

        message_count = session_doc.get("message_count", 0)
        turn_count = max(0, (message_count + 1) // 2)
        if turn_count >= MAX_DELETABLE_TURNS:
            return (
                jsonify(
                    {
                        "error": f"Cannot delete conversations with {MAX_DELETABLE_TURNS} or more turns",
                        "turn_count": turn_count,
                    }
                ),
                403,
            )

        chat_history_service.delete_chat_history(user_concept_id, session_id)

        return (
            jsonify(
                {
                    "status": "deleted",
                    "session_id": session_id,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error deleting chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/chat_session_links", methods=["GET", "POST"])
def chat_session_links():
    """Get or set concept links for a chat session.

    Stored on the chat_history session document as `session_links`.
    Links are many-to-many lists of concept_ids:
      - programmes
      - projects
      - activities
      - modalities
    """
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        requested_session_id = (
            request.args.get("session_id")
            if request.method == "GET"
            else (request.get_json(silent=True) or {}).get("session_id")
        )
        session_id = None
        if isinstance(requested_session_id, str) and requested_session_id.strip():
            session_id = requested_session_id.strip()
        else:
            session_id = session.get("session_id")

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)

        # Best-effort: ensure modality concepts exist so the UI can attach them.
        try:
            from ...vontology.utils_vontology import (
                ensure_conversation_modality_concepts,
            )

            ensure_conversation_modality_concepts()
        except Exception:
            pass

        if request.method == "GET":
            links = chat_history_service.get_chat_session_links(
                user_id=user_concept_id,
                session_id=session_id,
                namespace=namespace,
            )
            return (
                jsonify(
                    {
                        "status": "ok",
                        "session_id": session_id,
                        "session_links": links,
                    }
                ),
                200,
            )

        data = request.get_json(silent=True) or {}
        raw_links = data.get("session_links")
        if not isinstance(raw_links, dict):
            # Allow top-level key alternatives for callers.
            raw_links = {
                "programmes": data.get("programmes") or data.get("programme_ids"),
                "projects": data.get("projects") or data.get("project_ids"),
                "activities": data.get("activities") or data.get("activity_ids"),
                "modalities": data.get("modalities") or data.get("modality_ids"),
            }

        # Filter out unknown concept IDs (best-effort) so we don't persist stale references.
        try:
            from ...db.repositories.concepts_repository import ConceptsRepository

            def _flatten(values):
                if values is None:
                    return []
                if isinstance(values, str):
                    return [values]
                if isinstance(values, list):
                    return values
                return []

            all_ids = []
            for key in ("programmes", "projects", "activities", "modalities"):
                all_ids.extend(_flatten(raw_links.get(key)))

            all_ids = [
                v.strip()
                for v in all_ids
                if isinstance(v, str) and v.strip().startswith("#V#")
            ]
            if all_ids:
                found = set(
                    doc.get("concept_id")
                    for doc in ConceptsRepository.find(
                        {"concept_id": {"$in": list(set(all_ids))}},
                        {"concept_id": 1},
                    )
                    if isinstance(doc, dict) and isinstance(doc.get("concept_id"), str)
                )
            else:
                found = set()

            missing = sorted({cid for cid in set(all_ids) if cid not in found})
            if found:
                for key in ("programmes", "projects", "activities", "modalities"):
                    raw_links[key] = [
                        v.strip()
                        for v in _flatten(raw_links.get(key))
                        if isinstance(v, str)
                        and v.strip().startswith("#V#")
                        and v.strip() in found
                    ]
        except Exception:
            missing = []

        result = chat_history_service.set_chat_session_links(
            user_id=user_concept_id,
            session_id=session_id,
            session_links=raw_links,
            namespace=namespace,
        )

        if not result.get("matched"):
            return jsonify({"error": "Session not found"}), 404

        body = {
            "status": "updated" if result.get("updated") else "ok",
            "session_id": session_id,
            "session_links": result.get("session_links") or {},
        }
        if missing:
            body["missing_concepts"] = missing
        return jsonify(body), 200
    except Exception as e:
        print(f"Error updating chat session links: {e}")
        return jsonify({"error": str(e)}), 500


def _normalise_workflow_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#v#"):
        cleaned = "#V#" + cleaned[3:]
    elif not cleaned.startswith("#V#"):
        cleaned = f"#V#{cleaned.lstrip('#')}"
    return cleaned


def _coerce_onboarding_max_retries(value: Any) -> int:
    try:
        retries = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid max_retries") from exc
    if retries < 0 or retries > 10:
        raise ValueError("invalid max_retries")
    return retries


def _build_onboarding_inputs(
    *, member_name: str, request_payload: Mapping[str, Any]
) -> dict[str, Any]:
    raw_inputs = request_payload.get("inputs")
    inputs = dict(raw_inputs) if isinstance(raw_inputs, Mapping) else {}
    inputs.setdefault("member_name", member_name)
    inputs.setdefault("new_member_name", member_name)
    return inputs


def _resolve_onboarding_workflow_candidates(
    request_payload: Mapping[str, Any] | None,
) -> list[str]:
    payload = request_payload if isinstance(request_payload, Mapping) else {}
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(candidate: Any) -> None:
        normalised = _normalise_workflow_id(candidate)
        if not normalised or normalised in seen:
            return
        seen.add(normalised)
        candidates.append(normalised)

    _add(payload.get("workflow_id"))

    payload_workflow_ids = payload.get("workflow_ids")
    if isinstance(payload_workflow_ids, list):
        for candidate in payload_workflow_ids:
            _add(candidate)

    env_workflow_ids = os.getenv(_ONBOARDING_WORKFLOW_IDS_ENV, "")
    if isinstance(env_workflow_ids, str) and env_workflow_ids.strip():
        for candidate in env_workflow_ids.split(","):
            _add(candidate)

    # Discovery fallback avoids hard-coding workflow identifiers in route code.
    try:
        registry = build_workflow_registry_read_only()
        registry_ids = sorted(
            {
                normalised
                for workflow_id in registry.all_workflow_ids()
                for normalised in [_normalise_workflow_id(workflow_id)]
                if normalised
            }
        )
        for workflow_id in registry_ids:
            workflow_id_lc = workflow_id.lower()
            if any(term in workflow_id_lc for term in _ONBOARDING_WORKFLOW_KEYWORDS):
                _add(workflow_id)
    except Exception:
        pass

    return candidates


def _normalise_concept_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if not value.startswith("#V#"):
        value = f"#V#{value}"
    return value


def _normalise_history_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value.strip():
        return chat_history_service._coerce_datetime(value)
    return None


def _history_merge_key(message: dict) -> tuple | None:
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    content = message.get("content")
    author = message.get("author_user_id")
    ts = message.get("timestamp")
    if isinstance(ts, datetime):
        ts_key = ts.isoformat()
    elif isinstance(ts, str):
        ts_key = ts
    else:
        ts_key = None
    return (role, content, author, ts_key)


def _merge_shared_histories(
    owner_history: list[dict], invitee_history: list[dict]
) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple] = set()

    combined = (owner_history or []) + (invitee_history or [])
    for msg in combined:
        if not isinstance(msg, dict):
            continue
        key = _history_merge_key(msg)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        merged.append(msg)

    indexed = []
    for idx, msg in enumerate(merged):
        ts = _normalise_history_timestamp(msg.get("timestamp"))
        if ts is None:
            ts = datetime(1970, 1, 1, tzinfo=timezone.utc)
        indexed.append((ts, idx, msg))

    indexed.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in indexed]


def _apply_default_author(
    history: list[dict], author_user_id: str | None
) -> list[dict]:
    if not author_user_id:
        return history
    updated: list[dict] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user" and not msg.get("author_user_id"):
            patched = dict(msg)
            patched["author_user_id"] = author_user_id
            updated.append(patched)
        else:
            updated.append(msg)
    return updated


def _derive_namespace_for_user_org(
    user_concept_id: str | None, org_concept_id: str | None
) -> str | None:
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return None
    try:
        from ...services.namespace_service import derive_namespace

        user_slug = user_concept_id.strip()
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@", 1)[0]
        if "+" in user_slug:
            user_slug = user_slug.split("+", 1)[0]
        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        org_slug = None
        if isinstance(org_concept_id, str) and org_concept_id.strip():
            org_slug = org_concept_id.strip()
            if org_slug.startswith("#V#"):
                org_slug = org_slug[3:]
            if "@" in org_slug:
                org_slug = org_slug.split("@", 1)[0]
            if "+" in org_slug:
                org_slug = org_slug.split("+", 1)[0]
            org_slug = re.sub(r"[^a-z0-9]+", "_", org_slug.strip().lower()).strip("_")

        return (
            derive_namespace(user_slug, org_slug)
            if org_slug
            else derive_namespace(user_slug)
        )
    except Exception:
        return None


def _clean_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _resolve_history_request_scope_hints(
    *, user_concept_id: str, effective_context: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    requested_namespace = _clean_optional_text(request.args.get("namespace"))
    requested_org = _normalise_concept_id(request.args.get("organisation_concept_id"))
    effective_namespace = requested_namespace or _clean_optional_text(
        effective_context.get("namespace")
    )
    effective_org = (
        requested_org
        or _normalise_concept_id(effective_context.get("organisation_id"))
        or _normalise_concept_id(session.get("organisation_concept_id"))
    )

    if not effective_org:
        for namespace_candidate in (
            requested_namespace,
            effective_context.get("namespace"),
            session.get("namespace"),
        ):
            cleaned_candidate = _clean_optional_text(namespace_candidate)
            if not cleaned_candidate or "@" not in cleaned_candidate:
                continue
            effective_org = _normalise_concept_id(cleaned_candidate.split("@", 1)[-1])
            if effective_org:
                break

    derived_namespace = _derive_namespace_for_user_org(user_concept_id, effective_org)
    if derived_namespace and (
        not effective_namespace or "@" not in effective_namespace
    ):
        effective_namespace = derived_namespace

    if not effective_namespace:
        effective_namespace = chat_history_service.resolve_chat_history_namespace(
            user_concept_id
        )

    return effective_namespace, effective_org


def _resolve_shared_conversation_owner(
    *, user_concept_id: str | None, session_id: str | None
) -> tuple[str | None, dict | None]:
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return None, None
    if not isinstance(session_id, str) or not session_id.strip():
        return user_concept_id.strip(), None
    try:
        from ...services.shared_conversation_service import (
            get_accepted_invite_for_user_session,
            resolve_conversation_owner,
        )

        invite = get_accepted_invite_for_user_session(
            user_concept_id=user_concept_id.strip(),
            session_id=session_id.strip(),
        )
        if not isinstance(invite, dict):
            return user_concept_id.strip(), None
        owner = invite.get("conversation_owner_user_id") or invite.get(
            "inviter_user_id"
        )
        owner_id = _normalise_concept_id(owner)
        if not invite.get("conversation_owner_user_id"):
            try:
                resolved_owner = resolve_conversation_owner(
                    session_id=session_id.strip()
                )
                resolved_owner_id = _normalise_concept_id(resolved_owner)
                if resolved_owner_id:
                    owner_id = resolved_owner_id
            except Exception:
                pass
        return owner_id or user_concept_id.strip(), invite
    except Exception:
        return user_concept_id.strip(), None


def _collect_relationship_concept_ids(concept: dict) -> set[str]:
    relationships = concept.get("relationships") if isinstance(concept, dict) else None
    if not isinstance(relationships, dict):
        return set()
    found: set[str] = set()
    for value in relationships.values():
        if isinstance(value, str):
            cid = _normalise_concept_id(value)
            if cid:
                found.add(cid)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    cid = _normalise_concept_id(item)
                    if cid:
                        found.add(cid)
    return found


def _build_invitee_entry(concept: dict, *, role: str, match_details: dict) -> dict:
    from ...services.concept_service import enrich_concept_with_text_relations
    from ...vontology.utils_vontology import (
        get_concept_display_name_with_names_fallback,
    )

    enriched = enrich_concept_with_text_relations(concept)
    display_name = get_concept_display_name_with_names_fallback(enriched)
    return {
        "concept_id": concept.get("concept_id"),
        "name": display_name or concept.get("name") or "Unknown",
        "role": role,
        "match": match_details,
    }


@von_bp.route("/api/shared_conversations/invitees", methods=["GET"])
def get_shared_conversation_invitees():
    """List eligible invitees for a shared conversation.

    Returns organisation-scoped users, ordered by relevance to session links.
    """
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services import chat_history_service
        from ...services.organisation_membership_service import (
            get_organisation_members,
            get_user_memberships,
        )
        from ...services.concept_service import get_concept_by_concept_id
        from ...services.shared_conversation_service import get_invite_status_map

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        session_id = request.args.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        if not organisation_concept_id:
            return jsonify({"error": "No organisation context"}), 400

        memberships = get_user_memberships(user_concept_id)
        if not any(
            m.get("organisation_concept_id") == organisation_concept_id
            for m in memberships.get("memberships", [])
        ):
            return jsonify({"error": "Not authorised for organisation"}), 403

        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        session_links = chat_history_service.get_chat_session_links(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=namespace,
        )

        link_sets = {
            key: set(session_links.get(key) or [])
            for key in ("programmes", "projects", "activities", "modalities")
        }

        org_members = get_organisation_members(organisation_concept_id)
        candidates = []
        invitee_ids: list[str] = []
        for member in org_members.get("members", []):
            candidate_id = member.get("user_concept_id")
            candidate_id = _normalise_concept_id(candidate_id)
            if not candidate_id or candidate_id == user_concept_id:
                continue
            concept = get_concept_by_concept_id(concept_id=candidate_id)
            if not isinstance(concept, dict):
                continue

            related_ids = _collect_relationship_concept_ids(concept)
            match_details: dict[str, Any] = {
                key: sorted(link_sets[key] & related_ids) for key in link_sets
            }
            match_score = sum(len(vals) for vals in match_details.values())
            match_details["score"] = match_score

            entry = _build_invitee_entry(
                concept,
                role=member.get("role") or "member",
                match_details=match_details,
            )
            candidates.append(entry)
            invitee_ids.append(candidate_id)

        debug_payload = None
        if request.args.get("debug") == "1" and session.get("role_in_org") == "admin":
            debug_payload = {
                "user_concept_id": user_concept_id,
                "organisation_concept_id": organisation_concept_id,
                "membership_org_ids": [
                    m.get("organisation_concept_id")
                    for m in memberships.get("memberships", [])
                    if m.get("organisation_concept_id")
                ],
                "org_member_count": len(org_members.get("members", [])),
                "candidate_count": len(candidates),
                "excluded_self": user_concept_id,
                "session_links": session_links,
            }

        status_map = get_invite_status_map(
            session_id=session_id, invitee_ids=invitee_ids
        )
        for entry in candidates:
            invitee_id = entry.get("concept_id")
            if invitee_id and invitee_id in status_map:
                entry["invite_status"] = status_map[invitee_id]

        candidates.sort(
            key=lambda item: (
                -(item.get("match", {}).get("score") or 0),
                (item.get("name") or "").lower(),
            )
        )

        return (
            jsonify(
                {
                    "invitees": candidates,
                    "total_count": len(candidates),
                    "session_links": session_links,
                    "ordered_by": "conversation_links",
                    "debug": debug_payload,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error listing invitees: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invite", methods=["POST"])
def invite_to_shared_conversation():
    """Create a shared conversation invite for a session."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.organisation_membership_service import (
            get_organisation_members,
            get_user_memberships,
        )
        from ...services.shared_conversation_service import create_invite
        from ...services.episode_logging_service import log_episode

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        invitee_concept_id = _normalise_concept_id(
            data.get("invitee_concept_id") or data.get("invitee_user_id")
        )

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        if not invitee_concept_id:
            return jsonify({"error": "invitee_concept_id required"}), 400

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        if not organisation_concept_id:
            return jsonify({"error": "No organisation context"}), 400

        memberships = get_user_memberships(user_concept_id)
        if not any(
            m.get("organisation_concept_id") == organisation_concept_id
            for m in memberships.get("memberships", [])
        ):
            return jsonify({"error": "Not authorised for organisation"}), 403

        org_members = get_organisation_members(organisation_concept_id)
        valid_ids = {
            _normalise_concept_id(m.get("user_concept_id"))
            for m in org_members.get("members", [])
        }
        if invitee_concept_id not in valid_ids:
            return jsonify({"error": "Invitee not in organisation"}), 403

        result = create_invite(
            session_id=session_id,
            inviter_user_id=user_concept_id,
            invitee_user_id=invitee_concept_id,
            organisation_concept_id=organisation_concept_id,
        )

        log_episode(
            episode_type="shared_conversation_invite_created",
            actor_user_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            session_id=session_id,
            related_invite_id=result.get("invite", {}).get("invite_id"),
            payload={
                "invitee_user_id": invitee_concept_id,
                "created": bool(result.get("created")),
            },
            status="created" if result.get("created") else "exists",
        )

        return jsonify({"status": "ok", **result}), 200
    except Exception as e:
        print(f"Error creating invite: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invites", methods=["GET"])
def list_shared_conversation_invites():
    """List incoming invites for the authenticated user."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.shared_conversation_service import list_invites_for_user

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        status = request.args.get("status") or "pending"
        session_id = request.args.get("session_id")

        invites = list_invites_for_user(
            user_concept_id=user_concept_id,
            status=status,
            direction="incoming",
            session_id=session_id,
        )

        # JVNAUTOSCI-1004/1011: Filter invites by current organisation using window session
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        if organisation_concept_id:
            invites = [
                invite
                for invite in invites
                if (
                    _normalise_concept_id(invite.get("organisation_concept_id"))
                    in (None, organisation_concept_id)
                )
            ]

        return jsonify({"invites": invites, "total_count": len(invites)}), 200
    except Exception as e:
        print(f"Error listing invites: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invites/respond", methods=["POST"])
def respond_shared_conversation_invite():
    """Accept or decline a shared conversation invite."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.shared_conversation_service import respond_to_invite
        from ...services.episode_logging_service import log_episode

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        invite_id = data.get("invite_id")
        action = data.get("action")
        if not isinstance(invite_id, str) or not invite_id.strip():
            return jsonify({"error": "invite_id required"}), 400

        updated = respond_to_invite(
            invite_id=invite_id.strip(),
            user_concept_id=user_concept_id,
            action=action or "",
        )
        if not updated:
            return jsonify({"error": "Invite not found"}), 404

        log_episode(
            episode_type="shared_conversation_invite_responded",
            actor_user_id=user_concept_id,
            organisation_concept_id=updated.get("organisation_concept_id"),
            session_id=updated.get("session_id"),
            related_invite_id=invite_id.strip(),
            payload={"action": action},
            status=updated.get("status"),
        )

        return jsonify({"status": "ok", "invite": updated}), 200
    except Exception as e:
        print(f"Error responding to invite: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/stream", methods=["GET"])
def stream_shared_conversation():
    """SSE endpoint for real-time shared conversation turn updates.

    Query params:
        session_id: The shared conversation session to subscribe to

    Requires:
        - Authenticated user
        - Accepted invite for the session (or ownership)

    Returns:
        SSE stream with events:
            - user_turn: A user message was added
            - assistant_turn: An assistant response was added
            - keepalive: Periodic ping to keep connection alive
    """
    from flask import Response, stream_with_context

    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.shared_conversation_service import (
            get_accepted_invite_for_user_session,
            resolve_conversation_owner,
        )
        from ...services.shared_conversation_stream_service import get_stream_service

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        session_id = request.args.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        # Verify access: must be owner or have accepted invite
        is_owner = False
        has_invite = False

        owner_id = resolve_conversation_owner(session_id=session_id)
        if owner_id == user_concept_id:
            is_owner = True
        else:
            invite = get_accepted_invite_for_user_session(
                user_concept_id=user_concept_id,
                session_id=session_id,
            )
            if isinstance(invite, dict):
                has_invite = True

        if not is_owner and not has_invite:
            return jsonify({"error": "Not authorized for this conversation"}), 403

        stream_service = get_stream_service()
        subscriber = stream_service.subscribe(
            session_id=session_id,
            user_concept_id=user_concept_id,
        )

        def generate():
            try:
                for event_data in stream_service.generate_events(subscriber):
                    yield event_data
            finally:
                stream_service.unsubscribe(subscriber)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )
    except Exception as e:
        print(f"Error in shared conversation stream: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/organisations/my_organisations", methods=["GET"])
def get_my_organisations():
    """
    Get list of organisations the user is member of.

    Returns: {organisations: [{concept_id, name, role}, ...], total_count}
    """
    try:
        from ...security.role_resolver import get_all_user_organisations, get_user_role
        from ...services.concept_service import get_concept_by_concept_id

        user_id = (
            session.get("user_id")
            or session.get("user_concept_id")
            or session.get("user_email")
        )
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401

        requested_user_concept_id = request.args.get("user_concept_id")
        user_concept_id = requested_user_concept_id or session.get("user_concept_id")
        user_email = session.get("user_email")

        def _normalise_relationships(rel):
            if not isinstance(rel, (dict, list)):
                return {}
            if isinstance(rel, list):
                out = {}
                for item in rel:
                    if not isinstance(item, dict):
                        continue
                    pred = item.get("predicate")
                    tgt = item.get("target")
                    if not pred or not tgt:
                        continue
                    out.setdefault(pred, [])
                    if isinstance(tgt, list):
                        out[pred].extend(tgt)
                    else:
                        out[pred].append(tgt)
                return out
            return rel or {}

        def _prettify_concept_id(concept_id: str) -> str:
            return concept_id.replace("#V#", "").replace("_", " ").title()

        # Derive a slug for stub role resolution.
        # Prefer identifiers that are stable/meaningful (concept ID or email) over
        # opaque auth subjects.
        slug_source = user_concept_id or user_email or user_id

        user_slug = str(slug_source)
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@", 1)[0]
        if "+" in user_slug:
            user_slug = user_slug.split("+", 1)[0]

        import re

        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        USER_PREF_ORG_PREDICATE = "#V#member_of_organisation"

        organisations = []

        # Prefer memberships stored on the selected/authenticated user concept.
        if isinstance(user_concept_id, str) and user_concept_id.strip():
            try:
                user_concept = get_concept_by_concept_id(concept_id=user_concept_id)
            except Exception:
                user_concept = None
            if isinstance(user_concept, dict):
                rel = _normalise_relationships(user_concept.get("relationships", {}))
                org_raw = rel.get(USER_PREF_ORG_PREDICATE)

                org_targets: list[str] = []
                if isinstance(org_raw, str) and org_raw:
                    org_targets = [org_raw]
                elif isinstance(org_raw, list):
                    org_targets = [t for t in org_raw if isinstance(t, str) and t]

                for org_cid in org_targets:
                    org_cid = org_cid if org_cid.startswith("#V#") else f"#V#{org_cid}"
                    org_slug = org_cid[3:] if org_cid.startswith("#V#") else org_cid
                    org_slug = org_slug.strip().lower().replace(" ", "_")
                    try:
                        role = get_user_role(user_slug, org_slug)
                    except Exception:
                        role = "member"

                    organisations.append(
                        {
                            "concept_id": org_cid,
                            "name": _prettify_concept_id(org_cid),
                            "role": role,
                        }
                    )

        # Fallback: stub role resolver mappings (Phase 1 hardcoded)
        if not organisations:
            org_roles = get_all_user_organisations(user_slug)
            for org_id, role in org_roles.items():
                concept_id = org_id if org_id.startswith("#V#") else f"#V#{org_id}"
                organisations.append(
                    {
                        "concept_id": concept_id,
                        "name": _prettify_concept_id(concept_id),
                        "role": role,
                    }
                )

        return (
            jsonify(
                {"organisations": organisations, "total_count": len(organisations)}
            ),
            200,
        )

    except Exception as e:
        print(f"Error getting organisations: {e}")
        return jsonify({"error": str(e)}), 500


def _handle_orchestrator_missing_fallback(
    llm_client,
    model_name,
    prompt_text,
    enhanced_context,
    request_id,
    gateway,
    auxiliary_llm_calls,
    llm_interaction,
) -> str:
    """Handle orchestrator missing fallback."""
    import time
    from typing import Mapping, cast
    from ...integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    # Heuristic for provider inference if needed
    def _infer_provider(model: str | None) -> str:
        if not model:
            return "unknown"
        if "gpt" in model.lower():
            return "openai"
        if "claude" in model.lower():
            return "anthropic"
        if "gemini" in model.lower():
            return "google"
        return "unknown"

    orchestrator_status = (
        current_app.config.get("INTERNAL_MCP_ORCHESTRATOR_STATUS") or {}
    )
    orch_state = orchestrator_status.get("state", "unknown")
    current_app.logger.warning(
        "[ORCHESTRATOR_FALLBACK] orchestrator is None "
        "(state=%s); falling back to direct LLM generate for request_id=%s",
        orch_state,
        request_id,
    )
    auxiliary_llm_calls.append(
        {
            "type": "orchestrator_unavailable_fallback",
            "orchestrator_state": orch_state,
            "orchestrator_status_error": orchestrator_status.get("error"),
        }
    )

    method_catalogue_for_fallback = None
    if gateway is not None and hasattr(gateway, "describe_methods"):
        try:
            described = gateway.describe_methods()
            if isinstance(described, Mapping):
                method_catalogue_for_fallback = described
        except Exception:
            method_catalogue_for_fallback = None

    fallback_requirement_state = (
        InternalMCPChatOrchestrator._derive_prompt_tool_requirements(
            prompt_text,
            method_catalogue=method_catalogue_for_fallback,
            context_messages=enhanced_context,
        )
    )
    unavailable_required_tools = list(
        fallback_requirement_state.get("unavailable_required_tools") or []
    )
    if unavailable_required_tools:
        auxiliary_llm_calls.append(
            annotate_python_decision_event(
                {
                    "type": "prompt_tool_requirements_preflight",
                    "stage": "fallback_direct_llm",
                    "required_tools": list(
                        cast(
                            list[str],
                            fallback_requirement_state.get("required_tools") or [],
                        )
                    ),
                    "unavailable_required_tools": list(unavailable_required_tools),
                },
                stage="fallback_direct_llm",
                component="internal_mcp_orchestrator",
                function="_derive_prompt_tool_requirements",
                decision_class="prompt_requirement_inference",
                decision_source="explicit_identifier_parse",
                changed_outcome=True,
                reason_code="explicit_prompt_tool_unavailable_in_fallback",
                possible_inappropriate_python_code_use=False,
            )
        )
    llm_start_perf = time.perf_counter()
    response_text = llm_client.generate(
        prompt_text, context=enhanced_context, model=model_name
    )
    llm_interaction["duration_ms"] = (time.perf_counter() - llm_start_perf) * 1000.0
    fallback_entry = {
        "type": "llm.generate",
        "model": model_name,
        "provider": _infer_provider(model_name),
        "duration_ms": llm_interaction["duration_ms"],
        "usage": None,
        "workflow": "von_generate",
        "stage": "fallback_direct_llm",
        "prompt": {
            "text": prompt_text,
            "char_count": len(prompt_text) if isinstance(prompt_text, str) else 0,
            "is_truncated": False,
        },
        "request": {"context": enhanced_context},
        "response": {
            "text": str(response_text),
            "char_count": len(str(response_text)),
            "is_truncated": False,
        },
    }
    _stamp_llm_call_timestamps(
        fallback_entry,
        duration_ms=llm_interaction["duration_ms"],
    )
    llm_interaction["calls"] = [fallback_entry]
    return response_text


def _perform_legacy_spoken_backfill(
    llm_client,
    model_name,
    prompt_text,
    presenter_mode_requested,
    presenter_channels,
    screen_text,
    has_tool_messages,
    narration_prompt_text,
    speech_info,
):
    """Perform legacy spoken backfill logic extracted from generate()."""
    import time

    def _coerce_spoken_text(text: object) -> str | None:
        if not text:
            return None
        if isinstance(text, str):
            return text
        text_attr = getattr(text, "text", None)
        if text_attr is not None:
            return str(text_attr)
        return str(text)

    spoken_backfill_status = "not_started"
    spoken_backfill_suppression_reason = None
    spoken_backfill_applied = False
    spoken_backfill_error_class = None
    spoken_backfill_second_pass_reason = None
    spoken_backfill_started_perf = time.perf_counter()

    presenter_channels_missing = (
        not isinstance(presenter_channels, dict) or not presenter_channels
    )

    needs_spoken_backfill = presenter_mode_requested and (
        presenter_channels_missing or not presenter_channels.get("spoken")
    )

    if needs_spoken_backfill:
        try:
            if not screen_text or len(screen_text.strip()) < 10:
                spoken_backfill_second_pass_reason = "insufficient_screen_text"
            else:
                timing_hint = None
                try:
                    speech = speech_info if isinstance(speech_info, dict) else {}
                    raw_settings = speech.get("settings")
                    settings = raw_settings if isinstance(raw_settings, dict) else {}
                    preferred = settings.get("preferred_speaking_seconds")
                    maximum = settings.get("max_speaking_seconds")
                    try:
                        preferred_int = (
                            int(preferred) if preferred is not None else None
                        )
                    except Exception:
                        preferred_int = None
                    try:
                        maximum_int = int(maximum) if maximum is not None else None
                    except Exception:
                        maximum_int = None
                    if preferred_int is not None:
                        preferred_int = max(1, min(preferred_int, 600))
                    if maximum_int is not None:
                        maximum_int = max(1, min(maximum_int, 600))
                    effective_preferred = preferred_int
                    if preferred_int is not None and maximum_int is not None:
                        effective_preferred = min(preferred_int, maximum_int)
                    if effective_preferred is not None or maximum_int is not None:
                        timing_hint = f"Speech timing hint: preferred={effective_preferred!r}, max={maximum_int!r}."
                except Exception:
                    pass

                narration_system = (
                    "You are Von. Produce a short talk track for text-to-speech. "
                    "Return ONLY one block: <spoken>...</spoken>. "
                    "Do not include <screen>. Do not include code blocks. "
                    + ("\n\n" + timing_hint if timing_hint else "")
                    + (
                        "\n\nVON CHAT NARRATION PROMPT:\n" + narration_prompt_text
                        if narration_prompt_text
                        else ""
                    )
                )
                narration_user = f"User: {prompt_text}\n\nContent: {screen_text}"
                narration_response = _llm_generate_spoken_backfill(
                    llm_client, narration_system, narration_user, model_name
                )
                spoken_fallback = _coerce_spoken_text(narration_response)
                if not spoken_fallback:
                    spoken_fallback = _coerce_spoken_text(screen_text)
                if spoken_fallback:
                    base_channels = (
                        dict(presenter_channels)
                        if isinstance(presenter_channels, dict)
                        else {}
                    )
                    base_channels["screen"] = screen_text
                    base_channels["spoken"] = spoken_fallback
                    base_channels["format"] = "narration_fallback_v1"
                    presenter_channels = base_channels
                    spoken_backfill_applied = True
        except Exception as exc:
            spoken_backfill_error_class = type(exc).__name__

    latency = (time.perf_counter() - spoken_backfill_started_perf) * 1000.0
    if not presenter_mode_requested:
        spoken_backfill_status = "skipped"
        spoken_backfill_suppression_reason = "presenter_mode_disabled"
    elif not needs_spoken_backfill:
        spoken_backfill_status = "skipped"
        spoken_backfill_suppression_reason = "not_required"
    elif spoken_backfill_applied:
        spoken_backfill_status = "success"
    elif spoken_backfill_error_class:
        spoken_backfill_status = "failure"
        spoken_backfill_suppression_reason = "model_error"
    else:
        spoken_backfill_status = "no_op"
        spoken_backfill_suppression_reason = (
            spoken_backfill_second_pass_reason or "no_spoken_generated"
        )

    return (
        presenter_channels,
        spoken_backfill_status,
        spoken_backfill_suppression_reason,
        latency,
    )


def _llm_generate_screen_backfill(llm_client, system, user, model):
    return llm_client.generate(
        prompt="Generate <screen> display content",
        context=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=model,
    )


def _llm_generate_spoken_backfill(llm_client, system, user, model):
    return llm_client.generate(
        prompt="Generate <spoken> talk track",
        context=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=model,
    )


def _llm_generate_buttonify(llm_client, prompt, model):
    return llm_client.generate(
        prompt=prompt,
        context=[],
        model=model,
    )


def _contains_openai_quota_error(message: object) -> bool:
    raw_message = str(message) if message is not None else ""
    lowered = raw_message.lower()
    return (
        "insufficient_quota" in lowered
        or "quota_exhausted" in lowered
        or ("openai quota" in lowered and "exhausted" in lowered)
    )
