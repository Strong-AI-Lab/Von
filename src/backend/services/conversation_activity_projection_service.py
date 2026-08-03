"""Deterministic activity projection into a conversation carrier.

The conversation situation is useful only when it exists independently of a
model choosing to emit a private sidecar. This module projects a small,
audience-safe background-turn or workflow milestone into the existing
chat-history carrier:

* one bounded, idempotent exact observation; and
* a compact plain-text situation owned by this reducer.

The reducer never replaces situation text authored by a model, user, or reset.
Those carriers still receive the exact observation, so later turns can
reconcile the durable activity without replaying the originating transcript.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from . import chat_history_service

CONVERSATION_ACTIVITY_OBSERVATION_KIND = "conversation_turn_activity_milestone"
DURABLE_WORKFLOW_ACTIVITY_OBSERVATION_KIND = "durable_workflow_milestone"
CONVERSATION_ACTIVITY_SITUATION_SOURCE = "conversation_activity_projection"

_ACTIVITY_OBSERVATION_KINDS = frozenset(
    {
        CONVERSATION_ACTIVITY_OBSERVATION_KIND,
        DURABLE_WORKFLOW_ACTIVITY_OBSERVATION_KIND,
    }
)
_TERMINAL_ACTIVITY_STATUSES = frozenset({"completed", "failed", "cancelled"})

_OBSERVATION_SCHEMA_VERSION = "conversation_observation.v1"
_MAX_CAS_ATTEMPTS = 3
_MAX_SITUATION_CHARS = 4_000
_MAX_OBJECTIVE_CHARS = 1_000
_MAX_PROGRESS_MESSAGE_CHARS = 500
_MAX_PROGRESS_SUMMARY_CHARS = 900
_MAX_FACTS = 5
_PRIVATE_FACT_VISIBILITIES = frozenset(
    {
        "debug",
        "debugging",
        "hidden",
        "private",
        "private_redacted",
        "source_private",
    }
)


def _clean_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(0, limit - 3)].rstrip()}..."


def _bound_multiline(value: str, *, limit: int) -> str:
    cleaned = "\n".join(line.rstrip() for line in value.splitlines()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(0, limit - 3)].rstrip()}..."


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _as_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalise_fact_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _clean_text(value, limit=240)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items: list[str] = []
        for item in list(value)[:5]:
            normalised = _normalise_fact_value(item)
            if normalised:
                items.append(normalised)
        return ", ".join(items) if items else None
    return None


def _represented_progress_summary(
    facts: Sequence[Mapping[str, Any]] | None,
    *,
    allow_values: bool,
) -> str | None:
    """Render only already-authored, non-private scalar progress facts."""

    if not allow_values:
        return None
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw_fact in facts or ():
        if not isinstance(raw_fact, Mapping):
            continue
        if bool(raw_fact.get("redacted")):
            continue
        visibility = str(raw_fact.get("visibility") or "").strip().lower()
        if visibility in _PRIVATE_FACT_VISIBILITIES:
            continue
        status = str(raw_fact.get("status") or "").strip().lower()
        if status and status != "available":
            continue
        label = _clean_text(raw_fact.get("label"), limit=100)
        value = _normalise_fact_value(raw_fact.get("value"))
        if not label or not value:
            continue
        identity = (label.casefold(), value.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        lines.append(f"{label}: {value}")
        if len(lines) >= _MAX_FACTS:
            break
    if not lines:
        return None
    return _clean_text("; ".join(lines), limit=_MAX_PROGRESS_SUMMARY_CHARS)


def _workflow_label(workflow_id: str) -> str:
    cleaned = workflow_id.removeprefix("#V#")
    return re.sub(r"\s+", " ", cleaned.replace("_", " ")).strip().title()


def _milestone_identity(observation: Mapping[str, Any]) -> str:
    identity = {
        key: observation.get(key)
        for key in (
            "request_id",
            "activity_id",
            "workflow_id",
            "instance_id",
            "milestone",
            "activity_status",
            "progress_current",
            "progress_total",
            "progress_message",
            "progress_summary",
        )
        if observation.get(key) is not None
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reset_blocks_activity(
    situation: Mapping[str, Any] | None,
    *,
    originated_at: datetime | None,
) -> bool:
    if not isinstance(situation, Mapping):
        return False
    if str(situation.get("source") or "").strip() != "conversation_reset":
        return False
    reset_at = _as_utc_datetime(situation.get("updated_at"))
    # A missing origin or reset timestamp cannot prove that a late activity
    # belongs to the post-reset segment. Fail closed rather than repopulating it.
    if originated_at is None or reset_at is None:
        return True
    return originated_at <= reset_at


def _situation_is_reducer_owned(
    situation: Mapping[str, Any] | None,
    *,
    originated_at: datetime | None,
) -> bool:
    if situation is None:
        return True
    source = str(situation.get("source") or "").strip()
    if source == CONVERSATION_ACTIVITY_SITUATION_SOURCE:
        return True
    if source == "conversation_reset":
        return not _reset_blocks_activity(
            situation,
            originated_at=originated_at,
        )
    return False


def _render_activity_situation(
    observations: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    activities: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        if observation.get("kind") not in _ACTIVITY_OBSERVATION_KINDS:
            continue
        activity_id = _clean_text(observation.get("activity_id"), limit=160)
        instance_id = _clean_text(observation.get("instance_id"), limit=160)
        workflow_id = _clean_text(observation.get("workflow_id"), limit=240)
        activity_id = activity_id or instance_id
        if not activity_id:
            continue
        if activity_id not in activities:
            activities[activity_id] = {
                "activity_id": activity_id,
            }
            order.append(activity_id)
        activity = activities[activity_id]
        for field in (
            "request_id",
            "objective",
            "workflow_id",
            "instance_id",
            "activity_status",
            "milestone",
            "progress_current",
            "progress_total",
            "progress_message",
            "progress_summary",
        ):
            value = observation.get(field)
            if value is not None:
                activity[field] = value

    active = [
        activities[key]
        for key in order
        if str(activities[key].get("activity_status") or "").strip().lower()
        not in _TERMINAL_ACTIVITY_STATUSES
    ]
    # While work is active, the carrier should describe that work rather than
    # replaying earlier completed or failed objectives. Once nothing remains
    # active, retain only the newest terminal activity as the useful handoff.
    selected = active if active else [activities[order[-1]]] if order else []
    if not selected:
        return None, None

    # The route may replace a reducer-owned placeholder with model-authored
    # situation text only when the placeholder belongs exclusively to that
    # request.  A completing turn can render another still-active turn (or a
    # set of overlapping turns), so provenance must describe the rendered
    # selection rather than whichever milestone happened to trigger it.
    selected_request_ids = {
        request_id
        for activity in selected
        if (
            request_id := _clean_text(activity.get("request_id"), limit=160)
        )
    }
    exclusive_request_id = (
        next(iter(selected_request_ids))
        if len(selected_request_ids) == 1
        and all(
            _clean_text(activity.get("request_id"), limit=160)
            in selected_request_ids
            for activity in selected
        )
        else None
    )

    lines = ["Current conversation activity:"]
    for activity in selected:
        objective = _clean_text(activity.get("objective"), limit=_MAX_OBJECTIVE_CHARS)
        if objective:
            lines.append(f"- Objective: {objective}")
        status = _clean_text(activity.get("activity_status"), limit=80) or "active"
        current = _non_negative_int(activity.get("progress_current"))
        total = _non_negative_int(activity.get("progress_total"))
        progress = (
            f" ({current} of {total})"
            if current is not None and total is not None and total > 0
            else f" (progress {current})"
            if current is not None
            else ""
        )
        workflow_id = _clean_text(activity.get("workflow_id"), limit=240)
        instance_id = _clean_text(activity.get("instance_id"), limit=160)
        if workflow_id:
            identity = f" Instance {instance_id}." if instance_id else ""
            lines.append(
                f"- {_workflow_label(workflow_id)}: {status}{progress}.{identity}"
            )
        else:
            lines.append(
                f"- Turn activity: {status}{progress}. "
                f"Activity {activity['activity_id']}."
            )
        progress_message = _clean_text(
            activity.get("progress_message"),
            limit=_MAX_PROGRESS_MESSAGE_CHARS,
        )
        progress_summary = _clean_text(
            activity.get("progress_summary"),
            limit=_MAX_PROGRESS_SUMMARY_CHARS,
        )
        if progress_message:
            lines.append(f"  Current: {progress_message}")
        if progress_summary:
            lines.append(f"  Observed: {progress_summary}")
    return (
        _bound_multiline("\n".join(lines), limit=_MAX_SITUATION_CHARS),
        exclusive_request_id,
    )


def _project_conversation_activity(
    *,
    actor_user_id: str,
    history_owner_user_id: str | None,
    session_id: str,
    namespace: str | None,
    request_id: str,
    activity_id: str,
    objective: str | None,
    activity_status: str,
    milestone: str,
    observation_kind: str,
    workflow_id: str | None = None,
    instance_id: str | None = None,
    observed_at_utc: datetime | str | None = None,
    originated_at_utc: datetime | str | None = None,
    progress_current: int | None = None,
    progress_total: int | None = None,
    progress_message: str | None = None,
    represented_progress_facts: Sequence[Mapping[str, Any]] | None = None,
    effect_id: str | None = None,
) -> dict[str, Any]:
    """Project one activity milestone into its conversation carrier.

    ``namespace`` must be the namespace of the history owner selected at the
    trusted route boundary, not a model-supplied namespace. Represented fact
    values are omitted when the activity actor differs from the carrier owner;
    the exact workflow status remains visible without widening private data.
    """

    actor = _clean_text(actor_user_id, limit=160)
    owner = _clean_text(history_owner_user_id, limit=160) or actor
    session = _clean_text(session_id, limit=240)
    request = _clean_text(request_id, limit=160)
    activity = _clean_text(activity_id, limit=160)
    workflow = _clean_text(workflow_id, limit=240)
    instance = _clean_text(instance_id, limit=160)
    status = _clean_text(activity_status, limit=80)
    milestone_value = _clean_text(milestone, limit=80)
    if (
        actor is None
        or owner is None
        or session is None
        or request is None
        or activity is None
        or status is None
        or milestone_value is None
        or observation_kind not in _ACTIVITY_OBSERVATION_KINDS
    ):
        return {
            "updated": False,
            "reason": "invalid_activity_projection_identity",
        }

    observed_at = _as_utc_datetime(observed_at_utc) or datetime.now(UTC)
    originated_at = _as_utc_datetime(originated_at_utc)
    objective_value = _clean_text(objective, limit=_MAX_OBJECTIVE_CHARS)
    progress_message_value = _clean_text(
        progress_message,
        limit=_MAX_PROGRESS_MESSAGE_CHARS,
    )
    progress_summary = _represented_progress_summary(
        represented_progress_facts,
        allow_values=owner == actor,
    )

    try:
        initial_state = chat_history_service.get_chat_history_session_state(
            user_id=owner,
            session_id=session,
            namespace=namespace,
            include_history=False,
        )
    except chat_history_service.ChatHistoryServiceError as exc:
        return {
            "updated": False,
            "reason": "conversation_carrier_unavailable",
            "detail": str(exc),
        }
    if not isinstance(initial_state, Mapping):
        return {
            "updated": False,
            "reason": "conversation_carrier_not_found",
        }
    initial_situation = initial_state.get("conversation_situation")
    if _reset_blocks_activity(
        initial_situation if isinstance(initial_situation, Mapping) else None,
        originated_at=originated_at,
    ):
        return {
            "updated": False,
            "reason": "conversation_reset_after_activity_origin",
            "reset_guarded": True,
        }

    observation: dict[str, Any] = {
        "schema_version": _OBSERVATION_SCHEMA_VERSION,
        "observation_id": "",
        "kind": observation_kind,
        "observed_at_utc": _iso_utc(observed_at),
        "request_id": request,
        "activity_id": activity,
        "activity_status": status,
        "milestone": milestone_value,
    }
    if workflow:
        observation["workflow_id"] = workflow
    if instance:
        observation["instance_id"] = instance
    if originated_at is not None:
        observation["originated_at_utc"] = _iso_utc(originated_at)
    if effect_id_value := _clean_text(effect_id, limit=160):
        observation["effect_id"] = effect_id_value
    if objective_value:
        observation["objective"] = objective_value
    progress_current_value = _non_negative_int(progress_current)
    if progress_current_value is not None:
        observation["progress_current"] = progress_current_value
    progress_total_value = _non_negative_int(progress_total)
    if progress_total_value is not None:
        observation["progress_total"] = progress_total_value
    if progress_message_value:
        observation["progress_message"] = progress_message_value
    if progress_summary:
        observation["progress_summary"] = progress_summary
    observation["observation_id"] = _milestone_identity(observation)

    try:
        observation_outcome = (
            chat_history_service.append_chat_history_conversation_observation(
                user_id=owner,
                session_id=session,
                namespace=namespace,
                observation=observation,
            )
        )
    except chat_history_service.ChatHistoryServiceError as exc:
        return {
            "updated": False,
            "reason": "conversation_observation_projection_unavailable",
            "detail": str(exc),
        }
    if not bool(
        observation_outcome.get("updated") or observation_outcome.get("duplicate")
    ):
        return {
            "updated": False,
            "reason": str(
                observation_outcome.get("reason")
                or "conversation_observation_not_acknowledged"
            ),
            "observation": observation_outcome,
        }

    situation_outcome: dict[str, Any] = {
        "updated": False,
        "reason": "conversation_situation_not_reducer_owned",
    }
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        try:
            state = chat_history_service.get_chat_history_session_state(
                user_id=owner,
                session_id=session,
                namespace=namespace,
                include_history=False,
            )
        except chat_history_service.ChatHistoryServiceError as exc:
            situation_outcome = {
                "updated": False,
                "reason": "conversation_situation_readback_unavailable",
                "detail": str(exc),
            }
            break
        if not isinstance(state, Mapping):
            situation_outcome = {
                "updated": False,
                "reason": "conversation_carrier_not_found",
            }
            break
        raw_situation = state.get("conversation_situation")
        situation = raw_situation if isinstance(raw_situation, Mapping) else None
        if _reset_blocks_activity(situation, originated_at=originated_at):
            situation_outcome = {
                "updated": False,
                "reason": "conversation_reset_after_activity_origin",
                "reset_guarded": True,
            }
            break
        if not _situation_is_reducer_owned(
            situation,
            originated_at=originated_at,
        ):
            situation_outcome = {
                "updated": False,
                "reason": "conversation_situation_not_reducer_owned",
                "preserved": True,
            }
            break

        observations = state.get("conversation_observations")
        situation_text, situation_source_request_id = _render_activity_situation(
            observations if isinstance(observations, list) else [observation]
        )
        if not situation_text:
            situation_outcome = {
                "updated": False,
                "reason": "conversation_activity_text_unavailable",
            }
            break
        existing_text = (
            str(situation.get("text") or "").strip() if situation is not None else ""
        )
        if existing_text == situation_text:
            situation_outcome = {
                "updated": False,
                "reason": "conversation_situation_unchanged",
                "conversation_situation": dict(situation or {}),
            }
            break
        expected_revision = (
            situation.get("revision", 0) if isinstance(situation, Mapping) else 0
        )
        try:
            situation_outcome = (
                chat_history_service.set_chat_history_conversation_situation(
                    user_id=owner,
                    session_id=session,
                    namespace=namespace,
                    text=situation_text,
                    expected_revision=(
                        expected_revision
                        if isinstance(expected_revision, int)
                        and not isinstance(expected_revision, bool)
                        and expected_revision >= 0
                        else 0
                    ),
                    source=CONVERSATION_ACTIVITY_SITUATION_SOURCE,
                    updated_by=actor,
                    source_request_id=situation_source_request_id,
                )
            )
        except chat_history_service.ChatHistoryServiceError as exc:
            situation_outcome = {
                "updated": False,
                "reason": "conversation_situation_projection_unavailable",
                "detail": str(exc),
            }
            break
        if not bool(situation_outcome.get("conflict")):
            break

    return {
        "updated": bool(
            observation_outcome.get("updated") or situation_outcome.get("updated")
        ),
        "observation": observation_outcome,
        "situation": situation_outcome,
        "observation_id": observation["observation_id"],
        "projected_observation": dict(observation),
        "activity_id": activity,
        **({"instance_id": instance} if instance else {}),
        **({"workflow_id": workflow} if workflow else {}),
    }


def project_conversation_turn_activity(
    *,
    actor_user_id: str,
    history_owner_user_id: str | None,
    session_id: str,
    namespace: str | None,
    request_id: str,
    activity_id: str,
    objective: str | None,
    activity_status: str,
    milestone: str,
    observed_at_utc: datetime | str | None = None,
    originated_at_utc: datetime | str | None = None,
    progress_current: int | None = None,
    progress_total: int | None = None,
    progress_message: str | None = None,
    represented_progress_facts: Sequence[Mapping[str, Any]] | None = None,
    effect_id: str | None = None,
    workflow_id: str | None = None,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """Project progress for any conversation turn, workflow-backed or direct."""

    return _project_conversation_activity(
        actor_user_id=actor_user_id,
        history_owner_user_id=history_owner_user_id,
        session_id=session_id,
        namespace=namespace,
        request_id=request_id,
        activity_id=activity_id,
        objective=objective,
        activity_status=activity_status,
        milestone=milestone,
        observation_kind=CONVERSATION_ACTIVITY_OBSERVATION_KIND,
        observed_at_utc=observed_at_utc,
        originated_at_utc=originated_at_utc,
        progress_current=progress_current,
        progress_total=progress_total,
        progress_message=progress_message,
        represented_progress_facts=represented_progress_facts,
        effect_id=effect_id,
        workflow_id=workflow_id,
        instance_id=instance_id,
    )


def project_durable_workflow_activity(
    *,
    actor_user_id: str,
    history_owner_user_id: str | None,
    session_id: str,
    namespace: str | None,
    request_id: str,
    objective: str | None,
    workflow_id: str,
    instance_id: str,
    activity_status: str,
    milestone: str,
    observed_at_utc: datetime | str | None = None,
    originated_at_utc: datetime | str | None = None,
    progress_current: int | None = None,
    progress_total: int | None = None,
    progress_message: str | None = None,
    represented_progress_facts: Sequence[Mapping[str, Any]] | None = None,
    effect_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility entry point for durable workflow activity."""

    return _project_conversation_activity(
        actor_user_id=actor_user_id,
        history_owner_user_id=history_owner_user_id,
        session_id=session_id,
        namespace=namespace,
        request_id=request_id,
        activity_id=instance_id,
        objective=objective,
        activity_status=activity_status,
        milestone=milestone,
        observation_kind=DURABLE_WORKFLOW_ACTIVITY_OBSERVATION_KIND,
        observed_at_utc=observed_at_utc,
        originated_at_utc=originated_at_utc,
        progress_current=progress_current,
        progress_total=progress_total,
        progress_message=progress_message,
        represented_progress_facts=represented_progress_facts,
        effect_id=effect_id,
        workflow_id=workflow_id,
        instance_id=instance_id,
    )


__all__ = [
    "CONVERSATION_ACTIVITY_OBSERVATION_KIND",
    "CONVERSATION_ACTIVITY_SITUATION_SOURCE",
    "DURABLE_WORKFLOW_ACTIVITY_OBSERVATION_KIND",
    "project_conversation_turn_activity",
    "project_durable_workflow_activity",
]
