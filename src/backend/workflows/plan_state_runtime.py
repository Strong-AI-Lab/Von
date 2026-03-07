"""Shared long-horizon workflow plan-state contracts and runtime helpers."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Sequence

from .context_paths import resolve_context_path
from .execution_contracts import append_runtime_event

WORKFLOW_PLAN_STATE_POLICY_SCHEMA_VERSION = "workflow_plan_state_policy.v1"
WORKFLOW_STEP_CHECKPOINT_POLICY_SCHEMA_VERSION = (
    "workflow_step_checkpoint_policy.v1"
)
WORKFLOW_COMPLETION_GATE_SCHEMA_VERSION = "workflow_completion_gate.v1"
WORKFLOW_PLAN_STATE_RUNTIME_SCHEMA_VERSION = "workflow_plan_state_runtime.v1"

WORKFLOW_PLAN_STATE_ALLOWED_STATUSES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "blocked",
    "done",
)

WORKFLOW_PLAN_STATE_KEY = "workflow_plan_state"
WORKFLOW_PLAN_STATE_EVENTS_KEY = "workflow_plan_state_events"
LAST_WORKFLOW_PLAN_STATE_EVENT_KEY = "last_workflow_plan_state_event"
WORKFLOW_COMPLETION_GATE_KEY = "workflow_completion_gate"
LAST_WORKFLOW_COMPLETION_GATE_KEY = "last_workflow_completion_gate"


def _utc_iso(now_utc: datetime | None = None) -> str:
    value = now_utc if isinstance(now_utc, datetime) else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _append_context_event(
    *,
    context: MutableMapping[str, Any],
    key: str,
    last_key: str,
    event: Mapping[str, Any],
    max_entries: int | None = None,
) -> None:
    existing = context.get(key)
    if not isinstance(existing, list):
        existing = []
        context[key] = existing
    payload = dict(event)
    existing.append(payload)
    if isinstance(max_entries, int) and max_entries > 0 and len(existing) > max_entries:
        del existing[:-max_entries]
    context[last_key] = payload


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return default


def _normalise_positive_int(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int = 1,
) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}_not_int") from exc
    if parsed < minimum:
        raise ValueError(f"{field_name}_below_minimum")
    return parsed


def _normalise_non_negative_int(
    value: Any,
    *,
    field_name: str,
    default: int,
) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}_not_int") from exc
    if parsed < 0:
        raise ValueError(f"{field_name}_below_minimum")
    return parsed


def _normalise_string_list(value: Any) -> list[str]:
    raw_items: list[str] = []
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        raw_items = [item for item in value if isinstance(item, str)]
    result: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        item = _normalise_text(raw_item)
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _normalise_plan_item_status(value: Any, *, field_name: str) -> str:
    status = _normalise_text(value).lower()
    if not status:
        raise ValueError(f"{field_name}_missing")
    if status not in set(WORKFLOW_PLAN_STATE_ALLOWED_STATUSES):
        raise ValueError(f"{field_name}_invalid")
    return status


def _normalise_plan_items(
    value: Any,
    *,
    default_status: str,
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("plan_items_not_list")

    normalised: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(value):
        item_id = ""
        label = ""
        description = ""
        item_status = default_status
        if isinstance(raw_item, str):
            item_id = _normalise_text(raw_item)
            label = item_id
        elif isinstance(raw_item, Mapping):
            item_id = _normalise_text(
                raw_item.get("item_id") or raw_item.get("id") or raw_item.get("name")
            )
            label = _normalise_text(raw_item.get("label") or item_id)
            description = _normalise_text(raw_item.get("description"))
            if raw_item.get("default_status") is not None:
                item_status = _normalise_plan_item_status(
                    raw_item.get("default_status"),
                    field_name=f"plan_item_default_status_{index}",
                )
            elif raw_item.get("status") is not None:
                item_status = _normalise_plan_item_status(
                    raw_item.get("status"),
                    field_name=f"plan_item_status_{index}",
                )
        else:
            raise ValueError("plan_item_invalid")

        if not item_id:
            raise ValueError("plan_item_id_missing")
        if item_id in seen:
            raise ValueError("plan_item_id_duplicate")
        seen.add(item_id)
        normalised.append(
            {
                "item_id": item_id,
                "label": label or item_id,
                "description": description,
                "default_status": item_status,
            }
        )
    return normalised


def _normalise_plan_item_updates(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("plan_item_updates_not_list")

    updates: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(value):
        item_id = ""
        status = "done"
        label = ""
        if isinstance(raw_item, str):
            item_id = _normalise_text(raw_item)
        elif isinstance(raw_item, Mapping):
            item_id = _normalise_text(
                raw_item.get("item_id") or raw_item.get("id") or raw_item.get("name")
            )
            label = _normalise_text(raw_item.get("label"))
            status = _normalise_plan_item_status(
                raw_item.get("status") or "done",
                field_name=f"plan_item_update_status_{index}",
            )
        else:
            raise ValueError("plan_item_update_invalid")
        if not item_id:
            raise ValueError("plan_item_update_id_missing")
        if item_id in seen:
            raise ValueError("plan_item_update_duplicate")
        seen.add(item_id)
        payload = {"item_id": item_id, "status": status}
        if label:
            payload["label"] = label
        updates.append(payload)
    return updates


def _normalise_required_plan_item_statuses(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        result: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            item_id = _normalise_text(raw_key)
            if not item_id:
                raise ValueError("required_plan_item_id_missing")
            if item_id in result:
                raise ValueError("required_plan_item_id_duplicate")
            result[item_id] = _normalise_plan_item_status(
                raw_value,
                field_name=f"required_plan_item_status_{item_id}",
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = {}
        for raw_item in value:
            item_id = _normalise_text(raw_item)
            if not item_id:
                raise ValueError("required_plan_item_id_missing")
            if item_id in result:
                raise ValueError("required_plan_item_id_duplicate")
            result[item_id] = "done"
        return result
    raise ValueError("required_plan_item_statuses_invalid")


def normalise_workflow_plan_state_policy_spec(
    value: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("workflow_plan_state_policy_not_object")

    default_status = (
        _normalise_plan_item_status(
            value.get("default_item_status") or "pending",
            field_name="default_item_status",
        )
        if value.get("default_item_status") is not None
        else "pending"
    )
    return {
        "schema_version": WORKFLOW_PLAN_STATE_POLICY_SCHEMA_VERSION,
        "plan_items": _normalise_plan_items(
            value.get("plan_items") or value.get("items"),
            default_status=default_status,
        ),
        "default_item_status": default_status,
        "summary_context_keys": _normalise_string_list(
            value.get("summary_context_keys") or value.get("summary_keys")
        ),
        "cursor_context_keys": _normalise_string_list(
            value.get("cursor_context_keys")
            or value.get("cursor_keys")
            or value.get("resumable_cursor_context_keys")
        ),
        "summary_interval_steps": _normalise_non_negative_int(
            value.get("summary_interval_steps")
            or value.get("periodic_summary_interval_steps"),
            field_name="summary_interval_steps",
            default=0,
        ),
        "max_checkpoint_history": _normalise_positive_int(
            value.get("max_checkpoint_history") or value.get("max_checkpoints"),
            field_name="max_checkpoint_history",
            default=20,
        ),
    }


def normalise_workflow_step_checkpoint_policy_spec(
    value: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("workflow_step_checkpoint_policy_not_object")

    plan_item_updates = _normalise_plan_item_updates(
        value.get("plan_item_updates") or value.get("plan_items")
    )
    return {
        "schema_version": WORKFLOW_STEP_CHECKPOINT_POLICY_SCHEMA_VERSION,
        "plan_item_updates": plan_item_updates,
        "checkpoint_label": _normalise_text(
            value.get("checkpoint_label") or value.get("label")
        ),
        "summary_context_keys": _normalise_string_list(
            value.get("summary_context_keys") or value.get("summary_keys")
        ),
        "cursor_context_keys": _normalise_string_list(
            value.get("cursor_context_keys")
            or value.get("cursor_keys")
            or value.get("resumable_cursor_context_keys")
        ),
        "progress_message": _normalise_text(
            value.get("progress_message") or value.get("message")
        ),
        "force_summary": _normalise_bool(value.get("force_summary"), default=False),
    }


def normalise_workflow_completion_gate_spec(
    value: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("workflow_completion_gate_not_object")

    required_plan_item_statuses = _normalise_required_plan_item_statuses(
        value.get("required_plan_item_statuses")
        if "required_plan_item_statuses" in value
        else value.get("required_done_plan_items")
    )
    blocked_statuses = _normalise_string_list(value.get("blocked_statuses"))
    if blocked_statuses:
        blocked_statuses = [
            _normalise_plan_item_status(item, field_name="blocked_status")
            for item in blocked_statuses
        ]
    else:
        blocked_statuses = ["blocked"]

    return {
        "schema_version": WORKFLOW_COMPLETION_GATE_SCHEMA_VERSION,
        "required_plan_item_statuses": required_plan_item_statuses,
        "required_context_keys": _normalise_string_list(
            value.get("required_context_keys") or value.get("required_outputs")
        ),
        "blocked_statuses": blocked_statuses,
        "require_declared_plan_items_done": _normalise_bool(
            value.get("require_declared_plan_items_done"),
            default=True,
        ),
        "allow_missing_plan_state": _normalise_bool(
            value.get("allow_missing_plan_state"),
            default=False,
        ),
    }


def _declared_item_map(
    plan_state_policy: Mapping[str, Any] | None,
) -> dict[str, dict[str, str]]:
    policy = plan_state_policy if isinstance(plan_state_policy, Mapping) else {}
    result: dict[str, dict[str, str]] = {}
    for raw_item in policy.get("plan_items") or []:
        if not isinstance(raw_item, Mapping):
            continue
        item_id = _normalise_text(raw_item.get("item_id"))
        if not item_id:
            continue
        result[item_id] = {
            "label": _normalise_text(raw_item.get("label") or item_id),
            "description": _normalise_text(raw_item.get("description")),
            "default_status": _normalise_text(raw_item.get("default_status") or "pending")
            or "pending",
        }
    return result


def _coerce_plan_state_policy(
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return normalise_workflow_plan_state_policy_spec(value)
    except ValueError:
        return value if isinstance(value, dict) else dict(value)


def _coerce_step_checkpoint_policy(
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return normalise_workflow_step_checkpoint_policy_spec(value)
    except ValueError:
        return value if isinstance(value, dict) else dict(value)


def _coerce_completion_gate(
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return normalise_workflow_completion_gate_spec(value)
    except ValueError:
        return value if isinstance(value, dict) else dict(value)


def _ensure_plan_item(
    *,
    plan_state: MutableMapping[str, Any],
    item_id: str,
    declared_items: Mapping[str, Mapping[str, Any]],
    default_status: str,
    now_iso: str,
    label: str | None = None,
) -> MutableMapping[str, Any]:
    items_raw = plan_state.get("items")
    if not isinstance(items_raw, MutableMapping):
        items_raw = {}
        plan_state["items"] = items_raw

    item = items_raw.get(item_id)
    if not isinstance(item, MutableMapping):
        declared = (
            declared_items.get(item_id) if isinstance(declared_items, Mapping) else None
        )
        item = {
            "item_id": item_id,
            "label": label
            or _normalise_text((declared or {}).get("label") or item_id)
            or item_id,
            "description": _normalise_text((declared or {}).get("description")),
            "status": _normalise_text(
                (declared or {}).get("default_status") or default_status
            )
            or default_status,
            "updated_at_utc": now_iso,
            "last_state_id": None,
            "checkpoint_count": 0,
        }
        items_raw[item_id] = item

    order_raw = plan_state.get("item_order")
    if not isinstance(order_raw, list):
        order_raw = []
        plan_state["item_order"] = order_raw
    if item_id not in order_raw:
        order_raw.append(item_id)
    if label:
        item["label"] = label
    return item


def _capture_context_snapshot(
    *,
    context: Mapping[str, Any],
    keys: Sequence[str],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for raw_key in keys:
        key = _normalise_text(raw_key)
        if not key:
            continue
        found, value = resolve_context_path(context=context, path=key)
        if not found:
            continue
        snapshot[key] = deepcopy(value)
    return snapshot


def ensure_workflow_plan_state(
    *,
    context: MutableMapping[str, Any],
    workflow_id: str,
    definition_metadata: Mapping[str, Any] | None,
    now_utc: datetime | None = None,
) -> MutableMapping[str, Any] | None:
    raw_policy = (
        definition_metadata.get("plan_state_policy")
        if isinstance(definition_metadata, Mapping)
        else None
    )
    plan_state_policy = _coerce_plan_state_policy(raw_policy)

    existing = context.get(WORKFLOW_PLAN_STATE_KEY)
    if isinstance(existing, MutableMapping):
        if not existing.get("workflow_id"):
            existing["workflow_id"] = workflow_id
        if not existing.get("schema_version"):
            existing["schema_version"] = WORKFLOW_PLAN_STATE_RUNTIME_SCHEMA_VERSION
        declared_items = _declared_item_map(plan_state_policy)
        now_iso = _utc_iso(now_utc)
        default_status = "pending"
        if isinstance(plan_state_policy, Mapping):
            default_status = _normalise_text(
                plan_state_policy.get("default_item_status") or "pending"
            )
        for item_id, item_details in declared_items.items():
            _ensure_plan_item(
                plan_state=existing,
                item_id=item_id,
                declared_items=declared_items,
                default_status=default_status or "pending",
                now_iso=now_iso,
                label=_normalise_text(item_details.get("label")),
            )
        return existing

    if plan_state_policy is None:
        return None

    now_iso = _utc_iso(now_utc)
    default_status = _normalise_text(
        plan_state_policy.get("default_item_status") or "pending"
    )
    plan_state: MutableMapping[str, Any] = {
        "schema_version": WORKFLOW_PLAN_STATE_RUNTIME_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "started_at_utc": now_iso,
        "updated_at_utc": now_iso,
        "current_state": "",
        "checkpoint_count": 0,
        "summary_count": 0,
        "resume_count": 0,
        "item_order": [],
        "items": {},
        "checkpoints": [],
        "last_checkpoint": None,
        "last_summary": None,
        "declared_item_ids": [
            _normalise_text(item.get("item_id"))
            for item in plan_state_policy.get("plan_items") or []
            if isinstance(item, Mapping) and _normalise_text(item.get("item_id"))
        ],
    }
    declared_items = _declared_item_map(plan_state_policy)
    for item_id, item_details in declared_items.items():
        _ensure_plan_item(
            plan_state=plan_state,
            item_id=item_id,
            declared_items=declared_items,
            default_status=default_status or "pending",
            now_iso=now_iso,
            label=_normalise_text(item_details.get("label")),
        )

    context[WORKFLOW_PLAN_STATE_KEY] = plan_state
    return plan_state


def mark_workflow_plan_state_resume(
    *,
    context: MutableMapping[str, Any],
    workflow_id: str,
    definition_metadata: Mapping[str, Any] | None,
    current_state: str,
    retry_count: int = 0,
    now_utc: datetime | None = None,
) -> MutableMapping[str, Any] | None:
    plan_state = ensure_workflow_plan_state(
        context=context,
        workflow_id=workflow_id,
        definition_metadata=definition_metadata,
        now_utc=now_utc,
    )
    if not isinstance(plan_state, MutableMapping):
        return None

    now_iso = _utc_iso(now_utc)
    plan_state["resume_count"] = int(plan_state.get("resume_count") or 0) + 1
    plan_state["updated_at_utc"] = now_iso
    plan_state["current_state"] = _normalise_text(current_state)

    max_entries = int(
        (
            _coerce_plan_state_policy(
                definition_metadata.get("plan_state_policy", {})
                if isinstance(definition_metadata, Mapping)
                else {}
            )
            or {}
        ).get("max_checkpoint_history")
        or 20
    )
    event = {
        "status": "workflow_plan_state_resumed",
        "workflow_id": workflow_id,
        "state_id": _normalise_text(current_state) or None,
        "retry_count": int(retry_count),
        "resume_count": int(plan_state.get("resume_count") or 0),
        "occurred_at_utc": now_iso,
    }
    _append_context_event(
        context=context,
        key=WORKFLOW_PLAN_STATE_EVENTS_KEY,
        last_key=LAST_WORKFLOW_PLAN_STATE_EVENT_KEY,
        event=event,
        max_entries=max_entries,
    )
    append_runtime_event(context=context, event=event)
    return plan_state


def mark_workflow_plan_state_entry(
    *,
    context: MutableMapping[str, Any],
    workflow_id: str,
    state_id: str,
    definition_metadata: Mapping[str, Any] | None,
    state_metadata: Mapping[str, Any] | None,
    now_utc: datetime | None = None,
) -> MutableMapping[str, Any] | None:
    plan_state = ensure_workflow_plan_state(
        context=context,
        workflow_id=workflow_id,
        definition_metadata=definition_metadata,
        now_utc=now_utc,
    )
    if not isinstance(plan_state, MutableMapping):
        return None

    now_iso = _utc_iso(now_utc)
    raw_plan_policy = (
        definition_metadata.get("plan_state_policy")
        if isinstance(definition_metadata, Mapping)
        else None
    )
    plan_state_policy = _coerce_plan_state_policy(raw_plan_policy) or {}
    declared_items = _declared_item_map(plan_state_policy)
    default_status = _normalise_text(
        plan_state_policy.get("default_item_status") or "pending"
    )
    state_id_text = _normalise_text(state_id)
    items_raw = plan_state.get("items")
    item_exists = isinstance(items_raw, Mapping) and state_id_text in items_raw
    if declared_items and state_id_text not in declared_items and not item_exists:
        plan_state["current_state"] = state_id_text
        plan_state["updated_at_utc"] = now_iso
        return plan_state
    state_item = _ensure_plan_item(
        plan_state=plan_state,
        item_id=state_id_text,
        declared_items=declared_items,
        default_status=default_status or "pending",
        now_iso=now_iso,
    )
    if _normalise_text(state_item.get("status")) != "done":
        state_item["status"] = "in_progress"
        state_item["updated_at_utc"] = now_iso
        state_item["last_state_id"] = state_id_text

    plan_state["current_state"] = state_id_text
    plan_state["updated_at_utc"] = now_iso
    return plan_state


def apply_workflow_step_checkpoint(
    *,
    context: MutableMapping[str, Any],
    workflow_id: str,
    state_id: str,
    step_index: int,
    definition_metadata: Mapping[str, Any] | None,
    state_metadata: Mapping[str, Any] | None,
    blocked: bool = False,
    now_utc: datetime | None = None,
) -> MutableMapping[str, Any] | None:
    plan_state = ensure_workflow_plan_state(
        context=context,
        workflow_id=workflow_id,
        definition_metadata=definition_metadata,
        now_utc=now_utc,
    )
    if not isinstance(plan_state, MutableMapping):
        return None

    raw_plan_policy = (
        definition_metadata.get("plan_state_policy")
        if isinstance(definition_metadata, Mapping)
        else None
    )
    plan_state_policy = _coerce_plan_state_policy(raw_plan_policy) or {}
    raw_checkpoint_policy = (
        state_metadata.get("checkpoint_policy")
        if isinstance(state_metadata, Mapping)
        else None
    )
    checkpoint_policy = _coerce_step_checkpoint_policy(raw_checkpoint_policy) or {}

    now_iso = _utc_iso(now_utc)
    declared_items = _declared_item_map(plan_state_policy)
    default_status = _normalise_text(
        plan_state_policy.get("default_item_status") or "pending"
    )
    state_id_text = _normalise_text(state_id)
    plan_state["current_state"] = state_id_text
    plan_state["updated_at_utc"] = now_iso
    plan_state["checkpoint_count"] = int(plan_state.get("checkpoint_count") or 0) + 1
    checkpoint_count = int(plan_state.get("checkpoint_count") or 0)

    updates = checkpoint_policy.get("plan_item_updates")
    if not isinstance(updates, list):
        updates = []
    normalised_updates = [
        dict(item)
        for item in updates
        if isinstance(item, Mapping) and _normalise_text(item.get("item_id"))
    ]
    items_raw = plan_state.get("items")
    state_item_exists = isinstance(items_raw, Mapping) and state_id_text in items_raw
    if not normalised_updates and (
        not declared_items or state_id_text in declared_items or state_item_exists
    ):
        normalised_updates = [
            {
                "item_id": state_id_text,
                "status": "blocked" if blocked else "done",
            }
        ]

    cursor_keys = checkpoint_policy.get("cursor_context_keys")
    if not isinstance(cursor_keys, list) or not cursor_keys:
        cursor_keys = plan_state_policy.get("cursor_context_keys") or []
    summary_keys = checkpoint_policy.get("summary_context_keys")
    if not isinstance(summary_keys, list) or not summary_keys:
        summary_keys = plan_state_policy.get("summary_context_keys") or []

    cursor_snapshot = _capture_context_snapshot(context=context, keys=cursor_keys)
    summary_snapshot = _capture_context_snapshot(context=context, keys=summary_keys)

    summary_interval = int(plan_state_policy.get("summary_interval_steps") or 0)
    force_summary = bool(checkpoint_policy.get("force_summary"))
    should_emit_summary = force_summary or (
        summary_interval > 0
        and checkpoint_count % summary_interval == 0
        and bool(summary_snapshot)
    )
    last_summary_raw = plan_state.get("last_summary")
    last_summary: dict[str, Any] | None = None
    if isinstance(last_summary_raw, Mapping):
        last_summary = {
            str(key): value for key, value in last_summary_raw.items() if isinstance(key, str)
        }
    if should_emit_summary:
        summary_event: dict[str, Any] = {
            "status": "workflow_plan_state_summary",
            "workflow_id": workflow_id,
            "state_id": state_id_text,
            "step_index": int(step_index),
            "summary_snapshot": summary_snapshot,
            "occurred_at_utc": now_iso,
        }
        plan_state["summary_count"] = int(plan_state.get("summary_count") or 0) + 1
        plan_state["last_summary"] = summary_event
        last_summary = summary_event

    applied_updates: list[dict[str, Any]] = []
    for update in normalised_updates:
        item_id = _normalise_text(update.get("item_id"))
        if not item_id:
            continue
        item = _ensure_plan_item(
            plan_state=plan_state,
            item_id=item_id,
            declared_items=declared_items,
            default_status=default_status or "pending",
            now_iso=now_iso,
            label=_normalise_text(update.get("label")),
        )
        status = _normalise_text(
            update.get("status") or ("blocked" if blocked else "done")
        )
        item["status"] = status
        item["updated_at_utc"] = now_iso
        item["last_state_id"] = state_id_text
        item["checkpoint_count"] = int(item.get("checkpoint_count") or 0) + 1
        if cursor_snapshot:
            item["cursor_snapshot"] = deepcopy(cursor_snapshot)
        if summary_snapshot:
            item["summary_snapshot"] = deepcopy(summary_snapshot)
        applied_updates.append({"item_id": item_id, "status": status})

    checkpoint_event: dict[str, Any] = {
        "status": "workflow_plan_state_checkpoint",
        "workflow_id": workflow_id,
        "state_id": state_id_text,
        "step_index": int(step_index),
        "checkpoint_index": checkpoint_count,
        "checkpoint_label": _normalise_text(
            checkpoint_policy.get("checkpoint_label") or state_id
        )
        or state_id_text,
        "progress_message": _normalise_text(
            checkpoint_policy.get("progress_message")
            or checkpoint_policy.get("checkpoint_label")
            or state_id
        )
        or state_id_text,
        "plan_item_updates": applied_updates,
        "cursor_snapshot": cursor_snapshot,
        "summary_snapshot": summary_snapshot if should_emit_summary else {},
        "blocked": bool(blocked),
        "occurred_at_utc": now_iso,
    }
    if last_summary is not None:
        checkpoint_event["last_summary"] = deepcopy(last_summary)

    checkpoints_raw = plan_state.get("checkpoints")
    if not isinstance(checkpoints_raw, list):
        checkpoints_raw = []
        plan_state["checkpoints"] = checkpoints_raw
    checkpoints_raw.append(checkpoint_event)
    max_entries = int(plan_state_policy.get("max_checkpoint_history") or 20)
    if max_entries > 0 and len(checkpoints_raw) > max_entries:
        del checkpoints_raw[:-max_entries]
    plan_state["last_checkpoint"] = checkpoint_event

    _append_context_event(
        context=context,
        key=WORKFLOW_PLAN_STATE_EVENTS_KEY,
        last_key=LAST_WORKFLOW_PLAN_STATE_EVENT_KEY,
        event=checkpoint_event,
        max_entries=max_entries,
    )
    append_runtime_event(context=context, event=checkpoint_event)
    if last_summary is not None:
        _append_context_event(
            context=context,
            key=WORKFLOW_PLAN_STATE_EVENTS_KEY,
            last_key=LAST_WORKFLOW_PLAN_STATE_EVENT_KEY,
            event=last_summary,
            max_entries=max_entries,
        )
        append_runtime_event(context=context, event=last_summary)
    return plan_state


def compute_plan_state_progress(
    *,
    context: Mapping[str, Any],
    fallback_current: int,
    fallback_total: int,
    fallback_message: str | None,
) -> tuple[int, int, str | None]:
    plan_state = context.get(WORKFLOW_PLAN_STATE_KEY)
    if not isinstance(plan_state, Mapping):
        return int(fallback_current), int(fallback_total), fallback_message

    item_order = [
        _normalise_text(item_id)
        for item_id in plan_state.get("item_order") or []
        if _normalise_text(item_id)
    ]
    items = plan_state.get("items")
    if not item_order or not isinstance(items, Mapping):
        return int(fallback_current), int(fallback_total), fallback_message

    done_count = 0
    for item_id in item_order:
        raw_item = items.get(item_id)
        if not isinstance(raw_item, Mapping):
            continue
        if _normalise_text(raw_item.get("status")) == "done":
            done_count += 1

    message = fallback_message
    last_checkpoint = plan_state.get("last_checkpoint")
    if isinstance(last_checkpoint, Mapping):
        checkpoint_message = _normalise_text(last_checkpoint.get("progress_message"))
        if checkpoint_message:
            message = checkpoint_message

    return done_count, len(item_order), message


def evaluate_workflow_completion_gate(
    *,
    context: MutableMapping[str, Any],
    workflow_id: str,
    definition_metadata: Mapping[str, Any] | None,
    final_state: str,
    now_utc: datetime | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    raw_gate = (
        definition_metadata.get("completion_gate")
        if isinstance(definition_metadata, Mapping)
        else None
    )
    completion_gate = _coerce_completion_gate(raw_gate)
    if completion_gate is None:
        return True, None

    raw_plan_policy = (
        definition_metadata.get("plan_state_policy")
        if isinstance(definition_metadata, Mapping)
        else None
    )
    plan_state_policy = _coerce_plan_state_policy(raw_plan_policy) or {}
    plan_state = context.get(WORKFLOW_PLAN_STATE_KEY)
    plan_items = (
        plan_state.get("items")
        if isinstance(plan_state, Mapping) and isinstance(plan_state.get("items"), Mapping)
        else {}
    )

    blocking_reason_codes: list[str] = []
    missing_context_keys: list[str] = []
    unmet_plan_items: list[dict[str, str]] = []
    blocked_plan_items: list[dict[str, str]] = []

    required_context_keys = completion_gate.get("required_context_keys") or []
    for raw_key in required_context_keys:
        key = _normalise_text(raw_key)
        if not key:
            continue
        found, value = resolve_context_path(context=context, path=key)
        if not found or value in (None, False, "", [], {}, ()):
            missing_context_keys.append(key)

    declared_plan_items = [
        _normalise_text(raw_item.get("item_id"))
        for raw_item in plan_state_policy.get("plan_items") or []
        if isinstance(raw_item, Mapping) and _normalise_text(raw_item.get("item_id"))
    ]

    if not isinstance(plan_state, Mapping):
        if (
            completion_gate.get("required_plan_item_statuses")
            or (
                completion_gate.get("require_declared_plan_items_done")
                and bool(declared_plan_items)
            )
        ) and not bool(completion_gate.get("allow_missing_plan_state")):
            blocking_reason_codes.append("plan_state_missing")
    else:
        required_statuses = dict(completion_gate.get("required_plan_item_statuses") or {})
        if bool(completion_gate.get("require_declared_plan_items_done")):
            for item_id in declared_plan_items:
                if item_id and item_id not in required_statuses:
                    required_statuses[item_id] = "done"

        for item_id, required_status in required_statuses.items():
            raw_item = plan_items.get(item_id) if isinstance(plan_items, Mapping) else None
            actual_status = (
                _normalise_text(raw_item.get("status"))
                if isinstance(raw_item, Mapping)
                else ""
            )
            if actual_status != _normalise_text(required_status):
                unmet_plan_items.append(
                    {
                        "item_id": item_id,
                        "required_status": _normalise_text(required_status),
                        "actual_status": actual_status or "missing",
                    }
                )

        blocked_statuses = {
            _normalise_text(item)
            for item in completion_gate.get("blocked_statuses") or []
            if _normalise_text(item)
        }
        if isinstance(plan_items, Mapping):
            for item_id, raw_item in plan_items.items():
                if not isinstance(raw_item, Mapping):
                    continue
                actual_status = _normalise_text(raw_item.get("status"))
                if actual_status in blocked_statuses:
                    blocked_plan_items.append(
                        {
                            "item_id": _normalise_text(item_id),
                            "status": actual_status,
                        }
                    )

    if missing_context_keys:
        blocking_reason_codes.append("missing_required_context_keys")
    if unmet_plan_items:
        blocking_reason_codes.append("required_plan_items_unmet")
    if blocked_plan_items:
        blocking_reason_codes.append("blocked_plan_items_present")

    decision = {
        "schema_version": WORKFLOW_COMPLETION_GATE_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "final_state": _normalise_text(final_state),
        "safe_to_claim_completion": len(blocking_reason_codes) == 0,
        "blocking_reason_codes": blocking_reason_codes,
        "missing_context_keys": missing_context_keys,
        "unmet_plan_items": unmet_plan_items,
        "blocked_plan_items": blocked_plan_items,
        "checked_at_utc": _utc_iso(now_utc),
    }
    context[WORKFLOW_COMPLETION_GATE_KEY] = decision
    context[LAST_WORKFLOW_COMPLETION_GATE_KEY] = decision
    append_runtime_event(
        context=context,
        event={
            "status": "workflow_completion_gate",
            "workflow_id": workflow_id,
            "final_state": _normalise_text(final_state),
            "safe_to_claim_completion": decision["safe_to_claim_completion"],
            "blocking_reason_codes": list(blocking_reason_codes),
        },
    )
    return bool(decision["safe_to_claim_completion"]), decision


__all__ = [
    "LAST_WORKFLOW_COMPLETION_GATE_KEY",
    "LAST_WORKFLOW_PLAN_STATE_EVENT_KEY",
    "WORKFLOW_COMPLETION_GATE_KEY",
    "WORKFLOW_COMPLETION_GATE_SCHEMA_VERSION",
    "WORKFLOW_PLAN_STATE_ALLOWED_STATUSES",
    "WORKFLOW_PLAN_STATE_EVENTS_KEY",
    "WORKFLOW_PLAN_STATE_KEY",
    "WORKFLOW_PLAN_STATE_POLICY_SCHEMA_VERSION",
    "WORKFLOW_PLAN_STATE_RUNTIME_SCHEMA_VERSION",
    "WORKFLOW_STEP_CHECKPOINT_POLICY_SCHEMA_VERSION",
    "apply_workflow_step_checkpoint",
    "compute_plan_state_progress",
    "ensure_workflow_plan_state",
    "evaluate_workflow_completion_gate",
    "mark_workflow_plan_state_entry",
    "mark_workflow_plan_state_resume",
    "normalise_workflow_completion_gate_spec",
    "normalise_workflow_plan_state_policy_spec",
    "normalise_workflow_step_checkpoint_policy_spec",
]
