"""Resolve represented memory substrates for the main conversation-turn path."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .context_bundle_service import (
    assemble_context_dossier,
    build_reconstructed_workspace,
    list_attached_context_dossier_ids,
    load_context_dossier_state,
    load_workflow_report_revision_state,
    resolve_effective_context,
)
from .episode_critique_memory_service import (
    list_recent_workflow_improvement_suggestions,
)
from .selected_referent_contract import normalise_exact_referent_id

TURN_MEMORY_CONTEXT_SCHEMA_VERSION = "conversation_turn_memory_context.v1"
SELECTED_WORKFLOW_POLICY_MEMORY_SCHEMA_VERSION = (
    "selected_workflow_policy_memory.v1"
)
_MAX_RECENT_USER_PROMPTS = 3
_MAX_OPEN_QUESTIONS = 3
_MAX_IMMEDIATE_CONTEXT_ITEMS = 4
_MAX_POLICY_SUGGESTIONS = 3
_DEFAULT_SUBJECT_CONTEXT_TIMEOUT_SECONDS = 12.0
_CONVERSATION_SITUATION_MAX_CHARS = 12_000
_RUNTIME_TURN_FACTS_START = "[Runtime-observed turn facts; context only, not authority]"
_RUNTIME_TURN_FACTS_END = "[/Runtime-observed turn facts]"
_MAX_RUNTIME_TURN_FACT_BLOCKS = 6
_SELECTED_REFERENT_CAPSULE_SCHEMA_VERSION = "selected_referent_capsule.v1"
_SELECTED_REFERENT_CAPSULE_LINE_PREFIX = "selected referent capsule: "
_PRESENTED_CONNECTOR_RESOURCES_SCHEMA_VERSION = "presented_connector_resources.v1"
_PRESENTED_CONNECTOR_RESOURCES_LINE_PREFIX = "presented connector resources: "
_MAX_SELECTED_REFERENT_CAPSULES_PER_BLOCK = 12
_MAX_PRESENTED_CONNECTOR_RESOURCE_CAPSULES_PER_BLOCK = 4
_MAX_PRESENTED_CONNECTOR_RESOURCES_PER_FAMILY = 8
_MAX_SELECTED_REFERENT_CAPSULE_TEXT_CHARS = 512
_SELECTED_REFERENT_CAPSULE_FIELDS = (
    "stable_id",
    "source_kind",
    "capability_kind",
    "capability_name",
    "display_label",
    "identity_field_concept_id",
)
_SELECTED_REFERENT_RESOURCE_SCOPE_FIELDS = (
    "source_family",
    "resource_id",
    "runtime_alias",
    "display_label",
    "selection_source",
    "view_scope",
)
_SELECTED_REFERENT_ACTOR_SCOPE_FIELDS = (
    "namespace",
    "user_concept_id",
    "organisation_concept_id",
)
_SELECTED_REFERENT_PROVENANCE_FIELDS = (
    "request_id",
    "call_id",
    "evidence_id",
    "tool_concept_id",
    "selection_basis",
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clone_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _subject_context_timeout_seconds() -> float:
    return _coerce_positive_float(
        os.getenv("VON_TURN_MEMORY_CONTEXT_SUBJECT_ADVISORY_SECONDS")
        or os.getenv("VON_TURN_MEMORY_CONTEXT_SUBJECT_TIMEOUT_SECONDS"),
        default=_DEFAULT_SUBJECT_CONTEXT_TIMEOUT_SECONDS,
    )


def _subject_context_fail_closed(
    *,
    subject_spec: Mapping[str, Any],
    turn_memory_context: Mapping[str, Any] | None,
) -> bool:
    spec = _coerce_turn_memory_context_spec(turn_memory_context)
    strict = _coerce_bool(spec.get("strict"), default=False)
    is_primary = bool(subject_spec.get("is_primary"))
    if not is_primary:
        return strict
    return bool(
        strict
        or _normalise_strings(spec.get("effective_context_bundle_ids"))
        or _safe_str(spec.get("context_dossier_id"))
        or _safe_str(spec.get("report_revision_id"))
        or _coerce_bool(spec.get("materialise_context_dossier"), default=False)
    )


def _exception_subject_context(
    *,
    subject_spec: Mapping[str, Any],
    turn_memory_context: Mapping[str, Any] | None,
    exc: BaseException,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "subject_kind": (_safe_str(subject_spec.get("subject_kind")) or "").lower(),
        "subject_id": _safe_str(subject_spec.get("subject_id")),
        "subject_role": _safe_str(subject_spec.get("subject_role")) or "subject",
        "status": "unavailable",
        "fail_closed": _subject_context_fail_closed(
            subject_spec=subject_spec,
            turn_memory_context=turn_memory_context,
        ),
        "failure_reason": "turn_memory_context_subject_exception",
        "failed_substep": "resolve_subject_memory_context",
        "exception_type": type(exc).__name__,
        "error": str(exc),
        "elapsed_ms": round(elapsed_seconds * 1000.0, 1),
    }


def _normalise_strings(values: Any, *, limit: int | None = None) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if limit is not None and len(items) >= limit:
            break
    return items


def _truncate_text(value: Any, *, maximum: int = 240) -> str | None:
    text = _safe_str(value)
    if not text:
        return None
    if len(text) <= maximum:
        return text
    return text[:maximum].rstrip() + "..."


def _separate_runtime_turn_fact_blocks(text: Any) -> tuple[str, list[str]]:
    clean_text = text.strip() if isinstance(text, str) else ""
    if not clean_text:
        return "", []
    base_parts: list[str] = []
    blocks: list[str] = []
    cursor = 0
    while True:
        start = clean_text.find(_RUNTIME_TURN_FACTS_START, cursor)
        if start < 0:
            base_parts.append(clean_text[cursor:])
            break
        end = clean_text.find(_RUNTIME_TURN_FACTS_END, start)
        if end < 0:
            base_parts.append(clean_text[cursor:])
            break
        base_parts.append(clean_text[cursor:start])
        end += len(_RUNTIME_TURN_FACTS_END)
        blocks.append(clean_text[start:end].strip())
        cursor = end
    base = "\n\n".join(part.strip() for part in base_parts if part.strip())
    return base, blocks


def _runtime_turn_fact_block_request_id(block: str) -> str | None:
    for line in block.splitlines():
        if line.startswith("turn request id: "):
            return _safe_str(line.removeprefix("turn request id: "))
    return None


def _selected_referent_capsule_text(value: Any) -> str | None:
    text = _safe_str(value)
    if not text:
        return None
    return " ".join(text.split())[:_MAX_SELECTED_REFERENT_CAPSULE_TEXT_CHARS]


def _selected_referent_capsule_mapping(
    value: Any,
    *,
    allowed_fields: Sequence[str],
) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    selected = {
        field_name: cleaned
        for field_name in allowed_fields
        if (cleaned := _selected_referent_capsule_text(value.get(field_name)))
    }
    return selected or None


def _normalise_selected_referent_capsule(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("schema_version") != _SELECTED_REFERENT_CAPSULE_SCHEMA_VERSION:
        return None
    stable_id = normalise_exact_referent_id(value.get("stable_id"))
    selected = _selected_referent_capsule_mapping(
        value,
        allowed_fields=tuple(
            field_name
            for field_name in _SELECTED_REFERENT_CAPSULE_FIELDS
            if field_name != "stable_id"
        ),
    )
    if not stable_id or not selected or not all(
        selected.get(field_name)
        for field_name in (
            "source_kind",
            "capability_kind",
            "capability_name",
            "display_label",
        )
    ):
        return None
    capsule: dict[str, Any] = {
        "schema_version": _SELECTED_REFERENT_CAPSULE_SCHEMA_VERSION,
        "stable_id": stable_id,
        **selected,
    }
    resource_scope = _selected_referent_capsule_mapping(
        value.get("resource_scope"),
        allowed_fields=_SELECTED_REFERENT_RESOURCE_SCOPE_FIELDS,
    )
    if resource_scope:
        capsule["resource_scope"] = resource_scope
    actor_scope_ref = _selected_referent_capsule_mapping(
        value.get("actor_scope_ref"),
        allowed_fields=_SELECTED_REFERENT_ACTOR_SCOPE_FIELDS,
    )
    if actor_scope_ref:
        capsule["actor_scope_ref"] = actor_scope_ref
    provenance = _selected_referent_capsule_mapping(
        value.get("provenance"),
        allowed_fields=_SELECTED_REFERENT_PROVENANCE_FIELDS,
    )
    if provenance:
        capsule["provenance"] = provenance
    return capsule


def selected_referent_capsules_from_conversation_situation(
    conversation_situation: str | None,
) -> list[dict[str, Any]]:
    """Return bounded capsules from retained runtime-fact blocks.

    Only schema-versioned JSON lines emitted by this module are considered.
    Malformed or surprising state is ignored rather than treated as a scope
    or object selection.
    """

    _base, blocks = _separate_runtime_turn_fact_blocks(conversation_situation)
    capsules: list[dict[str, Any]] = []
    for block in blocks[-_MAX_RUNTIME_TURN_FACT_BLOCKS:]:
        block_capsules = 0
        for line in block.splitlines():
            if not line.startswith(_SELECTED_REFERENT_CAPSULE_LINE_PREFIX):
                continue
            if block_capsules >= _MAX_SELECTED_REFERENT_CAPSULES_PER_BLOCK:
                break
            raw_json = line.removeprefix(_SELECTED_REFERENT_CAPSULE_LINE_PREFIX)
            try:
                raw_capsule = json.loads(raw_json)
            except (TypeError, ValueError):
                continue
            capsule = _normalise_selected_referent_capsule(raw_capsule)
            if capsule is None:
                continue
            capsules.append(capsule)
            block_capsules += 1
    return capsules


def _normalise_presented_connector_resources(
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("schema_version") != _PRESENTED_CONNECTOR_RESOURCES_SCHEMA_VERSION:
        return None
    source_family = _selected_referent_capsule_text(value.get("source_family"))
    raw_resources = value.get("resources")
    if not source_family or not isinstance(raw_resources, Sequence) or isinstance(
        raw_resources, (str, bytes, bytearray)
    ):
        return None
    resources: list[dict[str, str]] = []
    seen_resource_ids: set[str] = set()
    for raw_resource in raw_resources[
        :_MAX_PRESENTED_CONNECTOR_RESOURCES_PER_FAMILY
    ]:
        resource = _selected_referent_capsule_mapping(
            raw_resource,
            allowed_fields=("resource_id", "display_label"),
        )
        if not resource or not all(
            resource.get(field_name)
            for field_name in ("resource_id", "display_label")
        ):
            continue
        resource_key = resource["resource_id"].casefold()
        if resource_key in seen_resource_ids:
            continue
        seen_resource_ids.add(resource_key)
        resources.append(resource)
    if not resources:
        return None
    return {
        "schema_version": _PRESENTED_CONNECTOR_RESOURCES_SCHEMA_VERSION,
        "source_family": source_family,
        "resources": resources,
    }


def presented_connector_resources_from_conversation_situation(
    conversation_situation: str | None,
) -> list[dict[str, Any]]:
    """Return only server-authored connector-resource presentation capsules."""

    _base, blocks = _separate_runtime_turn_fact_blocks(conversation_situation)
    capsules: list[dict[str, Any]] = []
    for block in blocks[-_MAX_RUNTIME_TURN_FACT_BLOCKS:]:
        block_capsules = 0
        for line in block.splitlines():
            if not line.startswith(_PRESENTED_CONNECTOR_RESOURCES_LINE_PREFIX):
                continue
            if (
                block_capsules
                >= _MAX_PRESENTED_CONNECTOR_RESOURCE_CAPSULES_PER_BLOCK
            ):
                break
            raw_json = line.removeprefix(
                _PRESENTED_CONNECTOR_RESOURCES_LINE_PREFIX
            )
            try:
                raw_capsule = json.loads(raw_json)
            except (TypeError, ValueError):
                continue
            capsule = _normalise_presented_connector_resources(raw_capsule)
            if capsule is None:
                continue
            capsules.append(capsule)
            block_capsules += 1
    return capsules


def latest_resource_scope_from_conversation_situation(
    conversation_situation: str | None,
    *,
    source_family: str | None = None,
    source_kind: str | None = None,
    capability_name: str | None = None,
    capability_kind: str | None = None,
) -> dict[str, str] | None:
    """Return the latest exactly matching retained resource scope.

    At least one family/kind/capability filter is required. This prevents an
    unrelated connector's most recent scope from becoming an implicit default.
    The result is conversational context only and grants no authority.
    """

    filters = {
        "source_family": _selected_referent_capsule_text(source_family),
        "source_kind": _selected_referent_capsule_text(source_kind),
        "capability_name": _selected_referent_capsule_text(capability_name),
        "capability_kind": _selected_referent_capsule_text(capability_kind),
    }
    filters = {key: value for key, value in filters.items() if value}
    if not filters:
        return None
    for capsule in reversed(
        selected_referent_capsules_from_conversation_situation(
            conversation_situation
        )
    ):
        resource_scope = capsule.get("resource_scope")
        if not isinstance(resource_scope, Mapping):
            continue
        if any(
            (
                resource_scope.get(field_name)
                if field_name == "source_family"
                else capsule.get(field_name)
            )
            != expected_value
            for field_name, expected_value in filters.items()
        ):
            continue
        return {
            str(key): str(value)
            for key, value in resource_scope.items()
            if key in _SELECTED_REFERENT_RESOURCE_SCOPE_FIELDS
            and isinstance(value, str)
            and value
        }
    return None


def _retain_runtime_turn_fact_blocks(
    blocks: Sequence[str],
    *,
    limit: int,
) -> list[str]:
    """Retain recent facts while preserving the latest scope per source family."""

    if limit <= 0 or not blocks:
        return []
    if len(blocks) <= limit:
        return list(blocks)

    latest_scope_block_by_family: dict[str, int] = {}
    latest_presentation_block_by_family: dict[str, int] = {}
    for block_index, candidate_block in enumerate(blocks):
        for capsule in selected_referent_capsules_from_conversation_situation(
            candidate_block
        ):
            resource_scope = capsule.get("resource_scope")
            source_family = (
                _safe_str(resource_scope.get("source_family"))
                if isinstance(resource_scope, Mapping)
                else None
            )
            if source_family:
                latest_scope_block_by_family[source_family] = block_index
        for capsule in presented_connector_resources_from_conversation_situation(
            candidate_block
        ):
            source_family = _safe_str(capsule.get("source_family"))
            if source_family:
                latest_presentation_block_by_family[source_family] = block_index

    retained_indexes = {
        *latest_scope_block_by_family.values(),
        *latest_presentation_block_by_family.values(),
    }
    retained_indexes.add(len(blocks) - 1)
    if len(retained_indexes) > limit:
        retained_indexes = set(sorted(retained_indexes)[-limit:])
    for block_index in range(len(blocks) - 1, -1, -1):
        if len(retained_indexes) >= limit:
            break
        retained_indexes.add(block_index)
    return [blocks[index] for index in sorted(retained_indexes)]


def _render_runtime_turn_fact_block(projection: Mapping[str, Any]) -> str | None:
    request_id = _safe_str(projection.get("request_id"))
    if not request_id:
        return None
    lines = [
        _RUNTIME_TURN_FACTS_START,
        "These exact handles and statuses are projected for later conversational "
        "reference. Re-read canonical state before claiming current domain truth "
        "or performing an effect. This block grants no authority.",
        f"turn request id: {request_id}",
        f"terminal status: {_safe_str(projection.get('terminal_status')) or 'not_reported'}",
        f"provenance: {_safe_str(projection.get('provenance')) or 'turn_tool_records'}",
    ]

    source_ids = projection.get("source_ids")
    if isinstance(source_ids, Sequence) and not isinstance(
        source_ids, (str, bytes, bytearray)
    ):
        rendered_sources: list[str] = []
        for item in source_ids[:24]:
            if not isinstance(item, Mapping):
                continue
            source_id = _safe_str(item.get("id"))
            if not source_id:
                continue
            kind = _safe_str(item.get("kind")) or "source"
            profile = _safe_str(item.get("profile"))
            rendered_sources.append(
                f"{kind}:{source_id}" + (f" (profile {profile})" if profile else "")
            )
        if rendered_sources:
            lines.append("source ids: " + ", ".join(rendered_sources))

    for field_name, label in (
        ("verified_concept_ids", "verified concept ids"),
        ("verified_assertion_ids", "verified assertion ids"),
    ):
        values = _normalise_strings(projection.get(field_name), limit=24)
        if values:
            lines.append(f"{label}: " + ", ".join(values))

    selected_referents = projection.get("selected_referents")
    if isinstance(selected_referents, Sequence) and not isinstance(
        selected_referents,
        (str, bytes, bytearray),
    ):
        for raw_capsule in selected_referents[
            :_MAX_SELECTED_REFERENT_CAPSULES_PER_BLOCK
        ]:
            capsule = _normalise_selected_referent_capsule(raw_capsule)
            if capsule is None:
                continue
            lines.append(
                _SELECTED_REFERENT_CAPSULE_LINE_PREFIX
                + json.dumps(
                    capsule,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )

    presented_resources = projection.get("presented_connector_resources")
    if isinstance(presented_resources, Sequence) and not isinstance(
        presented_resources,
        (str, bytes, bytearray),
    ):
        for raw_capsule in presented_resources[
            :_MAX_PRESENTED_CONNECTOR_RESOURCE_CAPSULES_PER_BLOCK
        ]:
            capsule = _normalise_presented_connector_resources(raw_capsule)
            if capsule is None:
                continue
            lines.append(
                _PRESENTED_CONNECTOR_RESOURCES_LINE_PREFIX
                + json.dumps(
                    capsule,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )

    relationships = projection.get("verified_relationships")
    if isinstance(relationships, Sequence) and not isinstance(
        relationships, (str, bytes, bytearray)
    ):
        rendered_relations: list[str] = []
        for relation in relationships[:12]:
            if not isinstance(relation, Mapping):
                continue
            source_id = _safe_str(relation.get("source_id"))
            predicate_id = _safe_str(relation.get("predicate_id"))
            target_id = _safe_str(relation.get("target_id"))
            if source_id and predicate_id and target_id:
                rendered_relations.append(
                    f"{source_id} --{predicate_id}--> {target_id}"
                )
        if rendered_relations:
            lines.append("verified relationships:")
            lines.extend(f"- {item}" for item in rendered_relations)

    effects = projection.get("effects")
    if isinstance(effects, Sequence) and not isinstance(
        effects, (str, bytes, bytearray)
    ):
        rendered_effects: list[str] = []
        for effect in effects[:12]:
            if not isinstance(effect, Mapping):
                continue
            identity = _safe_str(effect.get("effect_id")) or _safe_str(
                effect.get("tool")
            )
            status = _safe_str(effect.get("status"))
            if not identity or not status:
                continue
            details = [status]
            if isinstance(effect.get("changed"), bool):
                details.append(f"changed={str(effect.get('changed')).lower()}")
            mode = _safe_str(effect.get("mode"))
            if mode:
                details.append(f"mode={mode}")
            reconciliation_status = _safe_str(
                effect.get("reconciliation_status")
            )
            if reconciliation_status:
                details.append(f"reconciliation={reconciliation_status}")
            semantic_outcome = _safe_str(effect.get("semantic_outcome"))
            if semantic_outcome:
                details.append(f"semantic_outcome={semantic_outcome}")
            target_ids = _normalise_strings(effect.get("target_ids"), limit=12)
            if target_ids:
                details.append("targets=" + ",".join(target_ids))
            rendered_effects.append(f"{identity}: " + "; ".join(details))
        if rendered_effects:
            lines.append("effect receipts:")
            lines.extend(f"- {item}" for item in rendered_effects)

    workflows = projection.get("workflow_instances")
    if isinstance(workflows, Sequence) and not isinstance(
        workflows, (str, bytes, bytearray)
    ):
        rendered_workflows: list[str] = []
        for workflow in workflows[:12]:
            if not isinstance(workflow, Mapping):
                continue
            instance_id = _safe_str(workflow.get("instance_id"))
            status = _safe_str(workflow.get("status"))
            if not instance_id or not status:
                continue
            details = [status]
            workflow_id = _safe_str(workflow.get("workflow_id"))
            if workflow_id:
                details.append(f"workflow={workflow_id}")
            mode = _safe_str(workflow.get("mode"))
            if mode:
                details.append(f"mode={mode}")
            rendered_workflows.append(f"{instance_id}: " + "; ".join(details))
        if rendered_workflows:
            lines.append("workflow instances:")
            lines.extend(f"- {item}" for item in rendered_workflows)

    lines.append(_RUNTIME_TURN_FACTS_END)
    return "\n".join(lines)


def merge_conversation_situation_turn_projection(
    *,
    current_situation: str | None,
    model_situation: str | None,
    projection: Mapping[str, Any] | None,
) -> str | None:
    """Merge exact runtime facts with an optional model-authored sidecar.

    The text remains an actor-scoped conversation carrier. It is deliberately
    provenance-labelled and non-authoritative; the canonical tool/effect stores
    still decide whether an effect or represented assertion exists now.
    """

    selected_situation = (
        model_situation.strip()
        if isinstance(model_situation, str) and model_situation.strip()
        else (
            current_situation.strip()
            if isinstance(current_situation, str) and current_situation.strip()
            else ""
        )
    )
    if not isinstance(projection, Mapping):
        return selected_situation or None
    block = _render_runtime_turn_fact_block(projection)
    if not block:
        return selected_situation or None

    model_base, _model_blocks = _separate_runtime_turn_fact_blocks(selected_situation)
    _current_base, current_blocks = _separate_runtime_turn_fact_blocks(
        current_situation
    )
    blocks_by_request_id: dict[str, str] = {}
    unkeyed_blocks: list[str] = []
    # Runtime blocks are server projections, not model-authored situation text.
    # Preserve prior canonical blocks and add the current deterministic block;
    # never let a model sidecar introduce a reusable object or resource scope.
    for prior_block in [*current_blocks, block]:
        prior_request_id = _runtime_turn_fact_block_request_id(prior_block)
        if prior_request_id:
            blocks_by_request_id[prior_request_id] = prior_block
        elif prior_block not in unkeyed_blocks:
            unkeyed_blocks.append(prior_block)
    candidate_blocks = [
        *unkeyed_blocks,
        *blocks_by_request_id.values(),
    ]
    retained_blocks = _retain_runtime_turn_fact_blocks(
        candidate_blocks,
        limit=_MAX_RUNTIME_TURN_FACT_BLOCKS,
    )
    rendered_blocks = "\n\n".join(retained_blocks)
    separator_chars = 2 if model_base and rendered_blocks else 0
    base_budget = max(
        0,
        _CONVERSATION_SITUATION_MAX_CHARS - len(rendered_blocks) - separator_chars,
    )
    if len(model_base) > base_budget:
        marker = "\n[Earlier situation text truncated to retain exact turn facts.]"
        model_base = (
            model_base[: max(0, base_budget - len(marker))].rstrip() + marker
            if base_budget >= len(marker)
            else ""
        )
    merged = "\n\n".join(item for item in (model_base, rendered_blocks) if item)
    return merged[:_CONVERSATION_SITUATION_MAX_CHARS] or None


def _coerce_turn_memory_context_spec(
    turn_memory_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    spec = _clone_mapping(turn_memory_context)
    if "effective_context_bundle_ids" not in spec and isinstance(
        spec.get("context_bundle_ids"), (list, tuple)
    ):
        spec["effective_context_bundle_ids"] = list(spec.get("context_bundle_ids") or [])
    if "subject_kind" in spec:
        subject_kind = (_safe_str(spec.get("subject_kind")) or "").lower()
        spec["subject_kind"] = subject_kind or None
    return spec


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = _safe_str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _select_latest_dossier_id(dossier_ids: Sequence[str]) -> str | None:
    latest_id: str | None = None
    latest_updated_at: datetime | None = None
    for dossier_id in _normalise_strings(dossier_ids):
        state = load_context_dossier_state(dossier_id)
        if not isinstance(state, Mapping):
            continue
        updated_at = _parse_iso_datetime(state.get("updated_at_utc"))
        if latest_id is None:
            latest_id = dossier_id
            latest_updated_at = updated_at
            continue
        if updated_at is not None and (
            latest_updated_at is None or updated_at > latest_updated_at
        ):
            latest_id = dossier_id
            latest_updated_at = updated_at
    return latest_id


def _build_workspace_seed_immediate_context(
    *,
    prompt: str,
    recent_user_prompts: Sequence[str],
    conversation_session_id: str | None,
    subject_role: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "current_turn_prompt": prompt,
        "subject_role": subject_role,
    }
    if isinstance(conversation_session_id, str) and conversation_session_id.strip():
        payload["conversation_session_id"] = conversation_session_id.strip()
    recent_prompts = [
        prompt_text
        for prompt_text in _normalise_strings(
            recent_user_prompts,
            limit=_MAX_RECENT_USER_PROMPTS,
        )
        if prompt_text != prompt
    ]
    if recent_prompts:
        payload["recent_user_prompts"] = recent_prompts
    return payload


def _build_materialised_dossier_name(
    *,
    subject_role: str,
    subject_id: str,
) -> str:
    role_label = subject_role.replace("_", " ").strip() or "turn"
    return f"{role_label.title()} turn context dossier for {subject_id}"


def _resolve_subject_specs(
    *,
    turn_memory_context: Mapping[str, Any] | None,
    user_concept_id: str | None,
    org_concept_id: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = _coerce_turn_memory_context_spec(turn_memory_context)
    explicit_subject_id = _safe_str(spec.get("subject_id"))
    explicit_subject_kind = (_safe_str(spec.get("subject_kind")) or "concept").lower()
    requested_dossier_id = _safe_str(spec.get("context_dossier_id"))
    requested_report_revision_id = _safe_str(spec.get("report_revision_id"))

    if explicit_subject_id:
        if explicit_subject_kind not in {"concept", "workflow"}:
            return [], {
                "status": "unavailable",
                "failure_reason": "invalid_subject_kind",
                "fail_closed": True,
            }
        return [
            {
                "subject_id": explicit_subject_id,
                "subject_kind": explicit_subject_kind,
                "subject_role": "explicit_subject",
                "is_primary": True,
            }
        ], {}

    if requested_dossier_id:
        dossier_state = load_context_dossier_state(requested_dossier_id)
        if not isinstance(dossier_state, Mapping):
            return [], {
                "status": "unavailable",
                "failure_reason": "requested_context_dossier_missing",
                "fail_closed": True,
            }
        subject_id = _safe_str(dossier_state.get("subject_id"))
        subject_kind = (_safe_str(dossier_state.get("subject_kind")) or "").lower()
        if subject_id and subject_kind in {"concept", "workflow"}:
            return [
                {
                    "subject_id": subject_id,
                    "subject_kind": subject_kind,
                    "subject_role": "dossier_subject",
                    "is_primary": True,
                }
            ], {}
        return [], {
            "status": "unavailable",
            "failure_reason": "requested_context_dossier_subject_missing",
            "fail_closed": True,
        }

    if requested_report_revision_id:
        revision_state = load_workflow_report_revision_state(requested_report_revision_id)
        if not isinstance(revision_state, Mapping):
            return [], {
                "status": "unavailable",
                "failure_reason": "requested_report_revision_missing",
                "fail_closed": True,
            }
        dossier_id = _safe_str(revision_state.get("dossier_id"))
        if dossier_id:
            dossier_state = load_context_dossier_state(dossier_id)
            subject_id = _safe_str((dossier_state or {}).get("subject_id"))
            subject_kind = (_safe_str((dossier_state or {}).get("subject_kind")) or "").lower()
            if subject_id and subject_kind in {"concept", "workflow"}:
                return [
                    {
                        "subject_id": subject_id,
                        "subject_kind": subject_kind,
                        "subject_role": "report_revision_subject",
                        "is_primary": True,
                    }
                ], {}
        return [], {
            "status": "unavailable",
            "failure_reason": "requested_report_revision_subject_missing",
            "fail_closed": True,
        }

    subjects: list[dict[str, Any]] = []
    if isinstance(org_concept_id, str) and org_concept_id.strip():
        subjects.append(
            {
                "subject_id": org_concept_id.strip(),
                "subject_kind": "concept",
                "subject_role": "organisation",
                "is_primary": True,
            }
        )
    if (
        isinstance(user_concept_id, str)
        and user_concept_id.strip()
        and user_concept_id.strip() != (org_concept_id or "").strip()
    ):
        subjects.append(
            {
                "subject_id": user_concept_id.strip(),
                "subject_kind": "concept",
                "subject_role": "user",
                "is_primary": not subjects,
            }
        )
    return subjects, {}


def _resolve_subject_memory_context(
    *,
    subject_spec: Mapping[str, Any],
    turn_memory_context: Mapping[str, Any] | None,
    prompt: str,
    recent_user_prompts: Sequence[str],
    conversation_session_id: str | None,
    namespace: str | None,
    user_concept_id: str | None,
    org_concept_id: str | None,
) -> dict[str, Any]:
    spec = _coerce_turn_memory_context_spec(turn_memory_context)
    subject_id = _safe_str(subject_spec.get("subject_id"))
    subject_kind = (_safe_str(subject_spec.get("subject_kind")) or "").lower()
    subject_role = _safe_str(subject_spec.get("subject_role")) or "subject"
    is_primary = bool(subject_spec.get("is_primary"))
    strict = _coerce_bool(spec.get("strict"), default=False)
    apply_requested_state = is_primary

    requested_bundle_ids = (
        _normalise_strings(spec.get("effective_context_bundle_ids"))
        if apply_requested_state
        else []
    )
    requested_dossier_id = (
        _safe_str(spec.get("context_dossier_id")) if apply_requested_state else None
    )
    requested_report_revision_id = (
        _safe_str(spec.get("report_revision_id")) if apply_requested_state else None
    )
    materialise_context_dossier = bool(
        apply_requested_state
        and _coerce_bool(spec.get("materialise_context_dossier"), default=False)
    )
    materialise_report_revision = bool(
        apply_requested_state
        and _coerce_bool(spec.get("materialise_report_revision"), default=False)
    )
    build_workspace_requested = bool(
        apply_requested_state
        and _coerce_bool(spec.get("build_reconstructed_workspace"), default=False)
    )
    fail_closed = strict or bool(
        requested_bundle_ids
        or requested_dossier_id
        or requested_report_revision_id
        or materialise_context_dossier
    )

    if subject_kind not in {"concept", "workflow"} or not subject_id:
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable" if fail_closed else "none",
            "fail_closed": fail_closed,
            "failure_reason": (
                "subject_kind_and_subject_id_required" if fail_closed else None
            ),
        }

    resolution = resolve_effective_context(
        subject_kind=subject_kind,
        subject_id=subject_id,
        explicit_bundle_ids=requested_bundle_ids,
    )
    if not bool(resolution.get("success")):
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable" if fail_closed else "none",
            "fail_closed": fail_closed,
            "failure_reason": _safe_str(resolution.get("error"))
            or "context_bundle_resolution_failed",
        }

    diagnostics = _clone_mapping(resolution.get("diagnostics"))
    effective_context_bundle_ids = _normalise_strings(
        resolution.get("effective_context_bundle_ids")
    )
    effective_context_facet_ids = _normalise_strings(
        resolution.get("effective_context_facet_ids")
    )
    missing_bundle_ids = _normalise_strings(diagnostics.get("missing_bundle_ids"))
    if requested_bundle_ids and missing_bundle_ids:
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable",
            "fail_closed": True,
            "failure_reason": "requested_context_bundle_missing",
            "requested_context_bundle_ids": requested_bundle_ids,
            "missing_context_bundle_ids": missing_bundle_ids,
            "context_bundle_resolution": {
                "effective_context_bundle_ids": effective_context_bundle_ids,
                "effective_context_facet_ids": effective_context_facet_ids,
                "diagnostics": diagnostics,
            },
        }

    dossier_id = requested_dossier_id
    if not dossier_id:
        dossier_id = _select_latest_dossier_id(list_attached_context_dossier_ids(subject_id))
    dossier_state = (
        load_context_dossier_state(dossier_id) if isinstance(dossier_id, str) else None
    )
    if requested_dossier_id and not isinstance(dossier_state, Mapping):
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable",
            "fail_closed": True,
            "failure_reason": "requested_context_dossier_missing",
            "requested_context_dossier_id": requested_dossier_id,
        }

    report_revision_id = requested_report_revision_id
    report_revision_state = (
        load_workflow_report_revision_state(report_revision_id)
        if isinstance(report_revision_id, str)
        else None
    )
    if requested_report_revision_id and not isinstance(report_revision_state, Mapping):
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_role": subject_role,
            "status": "unavailable",
            "fail_closed": True,
            "failure_reason": "requested_report_revision_missing",
            "requested_report_revision_id": requested_report_revision_id,
        }

    materialised_dossier = False
    if materialise_context_dossier and not isinstance(dossier_state, Mapping):
        dossier_result = assemble_context_dossier(
            name=_build_materialised_dossier_name(
                subject_role=subject_role,
                subject_id=subject_id,
            ),
            subject_kind=subject_kind,
            subject_id=subject_id,
            effective_context_bundle_ids=effective_context_bundle_ids,
            open_questions=(
                "What prior context from the represented bundles matters most for this turn?",
                "Which open questions from this subject should influence the current response?",
            ),
            immediate_context=_build_workspace_seed_immediate_context(
                prompt=prompt,
                recent_user_prompts=recent_user_prompts,
                conversation_session_id=conversation_session_id,
                subject_role=subject_role,
            ),
            report_text=(
                "Initial main-turn memory dossier scaffold."
                if materialise_report_revision
                else None
            ),
            report_title="Main-turn memory dossier scaffold",
            report_summary={
                "subject_id": subject_id,
                "subject_kind": subject_kind,
                "source": "conversation_turn_memory_context_service",
            },
            namespace=namespace,
            user_id=user_concept_id,
            org_id=org_concept_id,
        )
        if not bool(dossier_result.get("success")):
            return {
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "subject_role": subject_role,
                "status": "unavailable",
                "fail_closed": True,
                "failure_reason": _safe_str(dossier_result.get("error"))
                or "context_dossier_materialisation_failed",
            }
        dossier_id = _safe_str(dossier_result.get("dossier_id"))
        if dossier_id:
            dossier_state = load_context_dossier_state(dossier_id)
        materialised_dossier = True
        if not requested_report_revision_id:
            report_revision_id = _safe_str(dossier_result.get("report_revision_id"))
            if report_revision_id:
                report_revision_state = load_workflow_report_revision_state(
                    report_revision_id
                )

    if not report_revision_state and isinstance(dossier_state, Mapping):
        report_revision_id = _safe_str(dossier_state.get("latest_report_revision_id"))
        if report_revision_id:
            report_revision_state = load_workflow_report_revision_state(report_revision_id)

    should_build_workspace = bool(
        build_workspace_requested
        or effective_context_bundle_ids
        or isinstance(dossier_state, Mapping)
    )
    reconstructed_workspace = None
    if should_build_workspace:
        workspace_result = build_reconstructed_workspace(
            subject_kind=subject_kind,
            subject_id=subject_id,
            question=prompt,
            task="Provide bounded authoritative turn memory context for the active turn.",
            dossier_id=dossier_id,
            report_revision_id=report_revision_id,
            effective_context_bundle_ids=effective_context_bundle_ids,
            immediate_context=(
                None
                if isinstance(dossier_state, Mapping)
                else _build_workspace_seed_immediate_context(
                    prompt=prompt,
                    recent_user_prompts=recent_user_prompts,
                    conversation_session_id=conversation_session_id,
                    subject_role=subject_role,
                )
            ),
        )
        if bool(workspace_result.get("success")):
            reconstructed_workspace = workspace_result.get("workspace")
        elif fail_closed:
            return {
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "subject_role": subject_role,
                "status": "unavailable",
                "fail_closed": True,
                "failure_reason": _safe_str(workspace_result.get("error"))
                or "reconstructed_workspace_unavailable",
            }

    status = "available"
    if not effective_context_bundle_ids and not dossier_state and not reconstructed_workspace:
        status = "none"

    return {
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "subject_role": subject_role,
        "status": status,
        "fail_closed": False,
        "materialised_context_dossier": materialised_dossier,
        "effective_context_bundle_ids": effective_context_bundle_ids,
        "effective_context_facet_ids": effective_context_facet_ids,
        "context_bundle_resolution": {
            "effective_context_bundle_ids": effective_context_bundle_ids,
            "effective_context_facet_ids": effective_context_facet_ids,
            "diagnostics": diagnostics,
        },
        "context_dossier_id": dossier_id,
        "context_dossier": dict(dossier_state) if isinstance(dossier_state, Mapping) else None,
        "report_revision_id": report_revision_id,
        "report_revision": (
            dict(report_revision_state)
            if isinstance(report_revision_state, Mapping)
            else None
        ),
        "reconstructed_workspace": (
            dict(reconstructed_workspace)
            if isinstance(reconstructed_workspace, Mapping)
            else None
        ),
        "workspace_fingerprint": (
            _safe_str((reconstructed_workspace or {}).get("workspace_fingerprint"))
            if isinstance(reconstructed_workspace, Mapping)
            else None
        ),
    }


def _resolve_subject_memory_context_bounded(
    *,
    subject_spec: Mapping[str, Any],
    turn_memory_context: Mapping[str, Any] | None,
    prompt: str,
    recent_user_prompts: Sequence[str],
    conversation_session_id: str | None,
    namespace: str | None,
    user_concept_id: str | None,
    org_concept_id: str | None,
) -> dict[str, Any]:
    """Resolve subject context with an advisory elapsed threshold.

    The former hard wait returned a synthetic unavailable context while the
    resolver continued in an abandoned daemon thread.  A slow represented
    memory read is still usable, so retain it and report that the advisory was
    crossed.
    """

    timeout_seconds = _subject_context_timeout_seconds()
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result_queue.put(
                (
                    "result",
                    _resolve_subject_memory_context(
                        subject_spec=subject_spec,
                        turn_memory_context=turn_memory_context,
                        prompt=prompt,
                        recent_user_prompts=recent_user_prompts,
                        conversation_session_id=conversation_session_id,
                        namespace=namespace,
                        user_concept_id=user_concept_id,
                        org_concept_id=org_concept_id,
                    ),
                )
            )
        except BaseException as exc:  # pragma: no cover - exercised through wrapper tests
            result_queue.put(("exception", exc))

    started_at = time.monotonic()
    thread = threading.Thread(
        target=_worker,
        name="turn-memory-context-subject",
        daemon=True,
    )
    thread.start()
    advisory_exceeded = False
    try:
        kind, payload = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        advisory_exceeded = True
        kind, payload = result_queue.get()

    elapsed = time.monotonic() - started_at
    if kind == "exception":
        return _exception_subject_context(
            subject_spec=subject_spec,
            turn_memory_context=turn_memory_context,
            exc=payload,
            elapsed_seconds=elapsed,
        )
    if isinstance(payload, Mapping):
        resolved_payload = dict(payload)
        resolved_payload.update(
            {
                "elapsed_time_enforcement": "advisory",
                "advisory_timeout_seconds": timeout_seconds,
                "advisory_exceeded": advisory_exceeded,
                "elapsed_seconds": round(elapsed, 3),
            }
        )
        return resolved_payload
    return _exception_subject_context(
        subject_spec=subject_spec,
        turn_memory_context=turn_memory_context,
        exc=TypeError("subject memory context resolver returned non-mapping result"),
        elapsed_seconds=elapsed,
    )


def build_turn_memory_context_state(
    *,
    prompt: str,
    recent_user_prompts: Sequence[str] = (),
    conversation_session_id: str | None = None,
    user_namespace: str | None = None,
    user_concept_id: str | None = None,
    org_concept_id: str | None = None,
    turn_memory_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    subject_specs, resolution_failure = _resolve_subject_specs(
        turn_memory_context=turn_memory_context,
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
    )
    spec = _coerce_turn_memory_context_spec(turn_memory_context)
    if resolution_failure:
        return {
            "schema_version": TURN_MEMORY_CONTEXT_SCHEMA_VERSION,
            "status": "unavailable",
            "fail_closed": bool(resolution_failure.get("fail_closed")),
            "failure_reason": resolution_failure.get("failure_reason"),
            "subject_contexts": [],
            "requested_memory_context": spec,
            "user_namespace": user_namespace,
            "conversation_session_id": conversation_session_id,
        }

    subject_contexts = [
        _resolve_subject_memory_context_bounded(
            subject_spec=subject_spec,
            turn_memory_context=spec,
            prompt=prompt,
            recent_user_prompts=recent_user_prompts,
            conversation_session_id=conversation_session_id,
            namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
        )
        for subject_spec in subject_specs
    ]

    fail_closed = False
    failure_reason = None
    for subject_context in subject_contexts:
        if subject_context.get("status") == "unavailable" and bool(
            subject_context.get("fail_closed")
        ):
            fail_closed = True
            failure_reason = _safe_str(subject_context.get("failure_reason"))
            break

    available_subjects = [
        item for item in subject_contexts if item.get("status") == "available"
    ]
    unavailable_subjects = [
        item for item in subject_contexts if item.get("status") == "unavailable"
    ]
    if fail_closed:
        status = "unavailable"
    elif available_subjects:
        status = "available"
    elif unavailable_subjects:
        status = "unavailable"
    else:
        status = "none"

    return {
        "schema_version": TURN_MEMORY_CONTEXT_SCHEMA_VERSION,
        "status": status,
        "fail_closed": fail_closed,
        "failure_reason": failure_reason,
        "requested_memory_context": spec,
        "user_namespace": user_namespace,
        "conversation_session_id": conversation_session_id,
        "subject_contexts": subject_contexts,
    }


def _render_workspace_lines(workspace: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    workspace_fingerprint = _safe_str(workspace.get("workspace_fingerprint"))
    if workspace_fingerprint:
        lines.append(f"- Workspace fingerprint: {workspace_fingerprint}")
    open_questions = _normalise_strings(
        workspace.get("open_questions"),
        limit=_MAX_OPEN_QUESTIONS,
    )
    if open_questions:
        lines.append("- Open questions: " + " | ".join(open_questions))
    immediate_context = workspace.get("immediate_context")
    if isinstance(immediate_context, Mapping):
        fragments: list[str] = []
        for key, value in list(immediate_context.items())[:_MAX_IMMEDIATE_CONTEXT_ITEMS]:
            key_text = _safe_str(key)
            value_text = _truncate_text(value, maximum=120)
            if key_text and value_text:
                fragments.append(f"{key_text}={value_text}")
        if fragments:
            lines.append("- Immediate context: " + " | ".join(fragments))
    evidence_receipts = workspace.get("evidence_receipts")
    if isinstance(evidence_receipts, Sequence) and not isinstance(
        evidence_receipts, (str, bytes, bytearray)
    ):
        lines.append(f"- Evidence receipts available: {len(list(evidence_receipts))}")
    return lines


def render_turn_memory_context_messages(
    state: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    if not isinstance(state, Mapping):
        return []
    if state.get("status") != "available":
        return []

    messages: list[dict[str, str]] = []
    for subject_context in state.get("subject_contexts") or []:
        if not isinstance(subject_context, Mapping):
            continue
        if subject_context.get("status") != "available":
            continue
        role_label = (
            _safe_str(subject_context.get("subject_role")) or "subject"
        ).replace("_", " ")
        subject_kind = _safe_str(subject_context.get("subject_kind")) or "subject"
        subject_id = _safe_str(subject_context.get("subject_id")) or "unknown"
        lines = [
            f"AUTHORITATIVE TURN MEMORY CONTEXT ({role_label.title()}):",
            f"- Subject: {subject_kind} {subject_id}",
        ]
        bundle_ids = _normalise_strings(subject_context.get("effective_context_bundle_ids"))
        if bundle_ids:
            lines.append("- Effective context bundles: " + ", ".join(bundle_ids[:8]))
        dossier_id = _safe_str(subject_context.get("context_dossier_id"))
        if dossier_id:
            lines.append(f"- Context dossier: {dossier_id}")
        report_revision_id = _safe_str(subject_context.get("report_revision_id"))
        if report_revision_id:
            lines.append(f"- Report revision: {report_revision_id}")
        workspace = subject_context.get("reconstructed_workspace")
        if isinstance(workspace, Mapping):
            lines.extend(_render_workspace_lines(workspace))
        lines.append(
            "Use this as represented working context for planning, tool use, and final response construction."
        )
        messages.append({"role": "system", "content": "\n".join(lines)})
    return messages


def summarise_turn_memory_context_for_lineage(
    state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(state, Mapping) or not state:
        return None
    subject_contexts = [
        item
        for item in (state.get("subject_contexts") or [])
        if isinstance(item, Mapping)
    ]
    if not subject_contexts and state.get("status") == "none":
        return {
            "status": "none",
            "subject_count": 0,
        }
    summary = {
        "status": _safe_str(state.get("status")) or "none",
        "fail_closed": bool(state.get("fail_closed")),
        "failure_reason": _safe_str(state.get("failure_reason")),
        "subject_count": len(subject_contexts),
        "available_subject_count": sum(
            1 for item in subject_contexts if item.get("status") == "available"
        ),
        "context_dossier_ids": [
            dossier_id
            for dossier_id in (
                _safe_str(item.get("context_dossier_id")) for item in subject_contexts
            )
            if dossier_id
        ],
        "workspace_fingerprints": [
            fingerprint
            for fingerprint in (
                _safe_str(item.get("workspace_fingerprint")) for item in subject_contexts
            )
            if fingerprint
        ],
    }
    return {key: value for key, value in summary.items() if value not in (None, [], {})}


def build_selected_workflow_policy_memory_state(
    *,
    selected_workflow_id: str | None,
    namespace: str | None = None,
    limit: int = _MAX_POLICY_SUGGESTIONS,
) -> dict[str, Any]:
    workflow_id = _safe_str(selected_workflow_id)
    if not workflow_id:
        return {
            "schema_version": SELECTED_WORKFLOW_POLICY_MEMORY_SCHEMA_VERSION,
            "status": "none",
            "selected_workflow_id": None,
            "suggestions": [],
        }

    suggestions = list_recent_workflow_improvement_suggestions(
        workflow_id,
        namespace=namespace,
        limit=max(1, min(int(limit), _MAX_POLICY_SUGGESTIONS)),
    )
    bounded_suggestions: list[dict[str, Any]] = []
    for item in suggestions[:_MAX_POLICY_SUGGESTIONS]:
        if not isinstance(item, Mapping):
            continue
        bounded_suggestions.append(
            {
                "memory_id": _safe_str(item.get("memory_id")),
                "suggestion_id": _safe_str(item.get("suggestion_id")),
                "category": _safe_str(item.get("category")),
                "priority": _safe_str(item.get("priority")),
                "target_surface": _safe_str(item.get("target_surface")),
                "title": _truncate_text(item.get("title"), maximum=140),
                "rationale": _truncate_text(item.get("rationale"), maximum=220),
                "request_id": _safe_str(item.get("request_id")),
            }
        )

    return {
        "schema_version": SELECTED_WORKFLOW_POLICY_MEMORY_SCHEMA_VERSION,
        "status": "available" if bounded_suggestions else "none",
        "selected_workflow_id": workflow_id,
        "namespace": _safe_str(namespace),
        "suggestion_count": len(bounded_suggestions),
        "suggestions": bounded_suggestions,
    }


def render_selected_workflow_policy_memory_messages(
    state: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    if not isinstance(state, Mapping):
        return []
    if state.get("status") != "available":
        return []
    workflow_id = _safe_str(state.get("selected_workflow_id")) or "selected workflow"
    suggestions = [
        item for item in (state.get("suggestions") or []) if isinstance(item, Mapping)
    ]
    if not suggestions:
        return []

    lines = [f"RECENT POLICY MEMORY FOR {workflow_id}:"]
    for item in suggestions[:_MAX_POLICY_SUGGESTIONS]:
        prefix = "/".join(
            segment
            for segment in (
                _safe_str(item.get("priority")),
                _safe_str(item.get("category")),
            )
            if segment
        )
        title = _safe_str(item.get("title")) or "Prior improvement signal"
        rationale = _safe_str(item.get("rationale"))
        if prefix:
            lines.append(f"- [{prefix}] {title}")
        else:
            lines.append(f"- {title}")
        if rationale:
            lines.append(f"- Rationale: {rationale}")
    lines.append(
        "Treat these as prior evaluated improvement signals. They do not override current-turn evidence or workflow authority."
    )
    return [{"role": "system", "content": "\n".join(lines)}]


def summarise_selected_workflow_policy_memory_for_lineage(
    state: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(state, Mapping) or not state:
        return None
    summary = {
        "status": _safe_str(state.get("status")) or "none",
        "selected_workflow_id": _safe_str(state.get("selected_workflow_id")),
        "suggestion_count": int(state.get("suggestion_count") or 0),
        "memory_ids": [
            memory_id
            for memory_id in (
                _safe_str(item.get("memory_id"))
                for item in (state.get("suggestions") or [])
                if isinstance(item, Mapping)
            )
            if memory_id
        ],
        "suggestion_ids": [
            suggestion_id
            for suggestion_id in (
                _safe_str(item.get("suggestion_id"))
                for item in (state.get("suggestions") or [])
                if isinstance(item, Mapping)
            )
            if suggestion_id
        ],
    }
    return {key: value for key, value in summary.items() if value not in (None, [], {})}


__all__ = [
    "SELECTED_WORKFLOW_POLICY_MEMORY_SCHEMA_VERSION",
    "TURN_MEMORY_CONTEXT_SCHEMA_VERSION",
    "build_selected_workflow_policy_memory_state",
    "build_turn_memory_context_state",
    "latest_resource_scope_from_conversation_situation",
    "presented_connector_resources_from_conversation_situation",
    "render_selected_workflow_policy_memory_messages",
    "render_turn_memory_context_messages",
    "selected_referent_capsules_from_conversation_situation",
    "summarise_selected_workflow_policy_memory_for_lineage",
    "summarise_turn_memory_context_for_lineage",
]
