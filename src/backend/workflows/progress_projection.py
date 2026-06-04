"""Generic workflow-authored progress fact projection support."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .context_paths import resolve_context_path
from .execution_contracts import snapshot_workflow_mapping


WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION = "workflow_progress_projection.v1"
WORKFLOW_PROGRESS_FACTS_KEY = "workflow_progress_facts"
LAST_WORKFLOW_PROGRESS_FACTS_KEY = "last_workflow_progress_facts"

_PROJECTION_METADATA_KEYS = (
    "progress_projection",
    "workflow_progress_projection",
    "thinking_card_progress_projection",
    "progress_facts",
)
_FACT_LIMIT = 12
_VALUE_LIMIT = 160
_DEBUG_VALUE_LIMIT = 320
_LIST_ITEM_LIMIT = 5
_PRIVATE_REDACTION_POLICIES = {
    "redact",
    "redacted",
    "hide",
    "hidden",
    "private",
    "private_redacted",
}


def _clean_text(value: Any, *, limit: int = _VALUE_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(0, limit - 3)].rstrip()}..."


def _normalise_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _normalise_projection_specs(metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(metadata, Mapping):
        return []

    raw_projection: Any = None
    for key in _PROJECTION_METADATA_KEYS:
        candidate = metadata.get(key)
        if candidate:
            raw_projection = candidate
            break

    if raw_projection is None:
        return []

    projection_payload: Mapping[str, Any] | Sequence[Any]
    if isinstance(raw_projection, Mapping):
        projection_payload = raw_projection
    elif isinstance(raw_projection, Sequence) and not isinstance(
        raw_projection, (str, bytes, bytearray)
    ):
        projection_payload = {"facts": list(raw_projection)}
    else:
        return []

    if isinstance(projection_payload, Mapping):
        schema_version = _clean_text(projection_payload.get("schema_version"))
        if schema_version and schema_version != WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION:
            return []
        raw_facts = projection_payload.get("facts")
    else:
        raw_facts = projection_payload

    if not isinstance(raw_facts, Sequence) or isinstance(
        raw_facts, (str, bytes, bytearray)
    ):
        return []

    specs: list[dict[str, Any]] = []
    for raw_fact in raw_facts:
        if not isinstance(raw_fact, Mapping):
            continue
        label = _clean_text(raw_fact.get("label"), limit=80)
        source_path = _clean_text(
            raw_fact.get("source_path")
            or raw_fact.get("path")
            or raw_fact.get("context_path")
            or raw_fact.get("value_from_context"),
            limit=240,
        )
        fact_id = _clean_text(
            raw_fact.get("fact_id")
            or raw_fact.get("id")
            or raw_fact.get("role")
            or label,
            limit=100,
        )
        if not fact_id or not label or not source_path:
            continue
        visibility = (
            _clean_text(raw_fact.get("visibility"), limit=80)
            or "thinking_card_default"
        )
        specs.append(
            {
                "fact_id": fact_id,
                "label": label,
                "source_path": source_path,
                "value_kind": (
                    _clean_text(raw_fact.get("value_kind"), limit=80) or "text"
                ),
                "visibility": visibility,
                "redaction_policy": _clean_text(
                    raw_fact.get("redaction_policy"), limit=100
                ),
                "contract_id": _clean_text(
                    raw_fact.get("contract_id")
                    or raw_fact.get("projection_contract_id")
                    or raw_fact.get("concept_id"),
                    limit=160,
                ),
                "sensitive": _normalise_bool(raw_fact.get("sensitive"))
                or _normalise_bool(raw_fact.get("private"))
                or str(raw_fact.get("sensitivity") or "").strip().lower()
                in {"private", "source_sensitive", "user_private"},
                "allow_raw": _normalise_bool(raw_fact.get("allow_raw")),
                "max_length": raw_fact.get("max_length"),
            }
        )
        if len(specs) >= _FACT_LIMIT:
            break
    return specs


def _resolve_projection_path(
    *,
    source_path: str,
    context_before: Mapping[str, Any],
    context_after: Mapping[str, Any],
    action_outputs: Mapping[str, Any],
    event: Mapping[str, Any],
) -> tuple[bool, Any, str]:
    roots = {
        "context": context_after,
        "context_after": context_after,
        "context_before": context_before,
        "action_outputs": action_outputs,
        "output_payload": action_outputs,
        "outputs": action_outputs,
        "event": event,
    }
    root_payload: dict[str, Any] = {key: value for key, value in roots.items()}
    found, value = resolve_context_path(context=root_payload, path=source_path)
    if found:
        return True, value, source_path
    found, value = resolve_context_path(context=context_after, path=source_path)
    if found:
        return True, value, f"context.{source_path}"
    return False, None, source_path


def _normalise_projected_value(
    value: Any,
    *,
    value_kind: str,
    max_length: Any,
) -> tuple[bool, Any, bool, str | None]:
    try:
        limit = int(max_length)
    except (TypeError, ValueError):
        limit = _VALUE_LIMIT
    limit = max(16, min(limit, _DEBUG_VALUE_LIMIT))

    if isinstance(value, bool):
        return True, value, False, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True, value, False, None
    if isinstance(value, str):
        cleaned = _clean_text(value, limit=limit)
        if not cleaned:
            return False, None, False, "empty_value"
        return True, cleaned, len(" ".join(value.split()).strip()) > len(cleaned), None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalised_items: list[Any] = []
        truncated = len(value) > _LIST_ITEM_LIMIT
        for item in list(value)[:_LIST_ITEM_LIMIT]:
            ok, item_value, item_truncated, _reason = _normalise_projected_value(
                item,
                value_kind=value_kind,
                max_length=max_length,
            )
            if ok:
                normalised_items.append(item_value)
            truncated = truncated or item_truncated
        if normalised_items:
            return True, normalised_items, truncated, None
        return False, None, False, "empty_list"
    if value_kind.strip().lower() in {"json", "object"} and isinstance(value, Mapping):
        try:
            rendered = json.dumps(snapshot_workflow_mapping(value), sort_keys=True)
        except Exception:
            return False, None, False, "unsupported_value"
        cleaned = _clean_text(rendered, limit=limit)
        if cleaned:
            return True, cleaned, len(rendered) > len(cleaned), None
    return False, None, False, "unsupported_value"


def build_progress_facts_for_step(
    *,
    workflow_id: str,
    state_id: str,
    action_id: str,
    workflow_metadata: Mapping[str, Any] | None,
    state_metadata: Mapping[str, Any] | None,
    context_before: Mapping[str, Any],
    context_after: Mapping[str, Any],
    action_outputs: Mapping[str, Any],
    state_attempt: int | None = None,
) -> list[dict[str, Any]]:
    """Evaluate represented progress projection metadata for a workflow step."""

    specs = [
        *_normalise_projection_specs(workflow_metadata),
        *_normalise_projection_specs(state_metadata),
    ]
    if not specs:
        return []

    base_event = {
        "workflow_id": workflow_id,
        "state_id": state_id,
        "action_id": action_id,
        "state_attempt": state_attempt,
    }
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for spec in specs:
        fact_key = (spec["fact_id"], spec["source_path"])
        if fact_key in seen:
            continue
        seen.add(fact_key)

        found, raw_value, resolved_path = _resolve_projection_path(
            source_path=spec["source_path"],
            context_before=context_before,
            context_after=context_after,
            action_outputs=action_outputs,
            event=base_event,
        )
        redaction_policy = spec.get("redaction_policy")
        sensitive = bool(spec.get("sensitive"))
        fact: dict[str, Any] = {
            "schema_version": WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION,
            "fact_id": spec["fact_id"],
            "label": spec["label"],
            "value_kind": spec["value_kind"],
            "visibility": spec["visibility"],
            "source_path": spec["source_path"],
            "resolved_path": resolved_path,
            "workflow_id": workflow_id,
            "state_id": state_id,
            "action_id": action_id,
            "present": bool(found),
            "status": "missing",
            "redacted": False,
            "truncated": False,
        }
        if spec.get("contract_id"):
            fact["contract_id"] = spec["contract_id"]
        if redaction_policy:
            fact["redaction_policy"] = redaction_policy

        if not found:
            fact["reason_code"] = "source_path_missing"
            facts.append(fact)
            continue

        policy_key = str(redaction_policy or "").strip().lower()
        if sensitive and not redaction_policy and not bool(spec.get("allow_raw")):
            fact.update(
                {
                    "status": "redacted",
                    "redacted": True,
                    "reason_code": "redaction_policy_missing",
                }
            )
            facts.append(fact)
            continue
        if policy_key in _PRIVATE_REDACTION_POLICIES:
            fact.update(
                {
                    "status": "redacted",
                    "redacted": True,
                    "reason_code": "redaction_policy",
                }
            )
            facts.append(fact)
            continue

        ok, value, truncated, reason = _normalise_projected_value(
            raw_value,
            value_kind=str(spec["value_kind"]),
            max_length=spec.get("max_length"),
        )
        if not ok:
            fact["reason_code"] = reason or "value_unavailable"
            facts.append(fact)
            continue
        fact.update(
            {
                "status": "available",
                "value": value,
                "truncated": bool(truncated),
            }
        )
        facts.append(fact)
        if len(facts) >= _FACT_LIMIT:
            break

    return facts


__all__ = [
    "LAST_WORKFLOW_PROGRESS_FACTS_KEY",
    "WORKFLOW_PROGRESS_FACTS_KEY",
    "WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION",
    "build_progress_facts_for_step",
]
