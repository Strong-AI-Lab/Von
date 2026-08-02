"""Bounded, human-readable projections of capability activity.

The projection is deliberately a view model, not a second execution record.
It retains exact identifiers needed for inspection while giving the ordinary
Thinking surface labels and role-labelled arguments that a person can read.
No database lookup is performed here: richer naming remains an optional later
enrichment rather than another source of latency or failure on the answer path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SEMANTIC_OPERATION_SCHEMA_VERSION = "thinking_semantic_operation.v1"
_MAX_TEXT_CHARS = 240
_MAX_ARGUMENTS = 8

_ARGUMENT_SPECS = (
    ("subject_concept_id", "subject", "Subject", "concept"),
    ("source_id", "subject", "Subject", "concept"),
    ("concept_id", "subject", "Subject", "concept"),
    ("predicate", "predicate", "Relation", "predicate"),
    ("predicate_id", "predicate", "Relation", "predicate"),
    ("predicate_ref", "predicate", "Relation", "predicate"),
    ("target_concept_id", "object", "Object", "concept"),
    ("target_id", "object", "Object", "concept"),
    ("target", "object", "Object", "concept"),
    ("target_text", "value", "Value", "text"),
    ("text", "value", "Value", "text"),
    ("workflow_id", "workflow", "Workflow", "workflow"),
    ("instance_id", "instance", "Workflow instance", "identifier"),
    ("name", "name", "Name", "text"),
)


def _clean_text(value: Any, *, limit: int = _MAX_TEXT_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(0, limit - 3)].rstrip()}..."


def _display_label(value: str) -> str:
    cleaned = value.removeprefix("#V#")
    cleaned = re.sub(r"[_\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() if cleaned else value


def _scalar_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return _clean_text(value)


def _argument_value(value: Any) -> str | int | float | bool | None:
    scalar = _scalar_value(value)
    if scalar is not None:
        return scalar
    if isinstance(value, Mapping):
        for key in ("concept_id", "predicate_id", "id", "value"):
            scalar = _scalar_value(value.get(key))
            if scalar is not None:
                return scalar
    return None


def _argument_projection(arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for key, role, label, value_kind in _ARGUMENT_SPECS:
        if role in seen_roles or key not in arguments:
            continue
        value = _argument_value(arguments.get(key))
        if value is None:
            continue
        display = (
            value
            if isinstance(value, str) and value_kind == "text"
            else _display_label(value)
            if isinstance(value, str)
            else str(value)
        )
        item: dict[str, Any] = {
            "role": role,
            "label": label,
            "source_argument": key,
            "value_kind": value_kind,
            "value": value,
            "display": display,
        }
        if isinstance(value, str) and value.startswith("#V#"):
            item["concept_id"] = value
        projected.append(item)
        seen_roles.add(role)
        if len(projected) >= _MAX_ARGUMENTS:
            break
    return projected


def _argument_by_role(
    arguments: list[dict[str, Any]], role: str
) -> dict[str, Any] | None:
    return next((item for item in arguments if item.get("role") == role), None)


def _relation_projection(
    arguments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    subject = _argument_by_role(arguments, "subject")
    predicate = _argument_by_role(arguments, "predicate")
    target = _argument_by_role(arguments, "object") or _argument_by_role(
        arguments, "value"
    )
    if subject is None or predicate is None or target is None:
        return None
    return {
        "subject": dict(subject),
        "predicate": dict(predicate),
        "object": dict(target),
    }


def _outcome_projection(
    result: Mapping[str, Any] | None,
    *,
    lifecycle_status: str,
    success: bool | None,
) -> dict[str, Any] | None:
    if result is None and lifecycle_status == "running":
        return None
    payload = result if isinstance(result, Mapping) else {}
    outcome: dict[str, Any] = {
        "status": lifecycle_status,
        "success": success,
    }
    for key in (
        "effect_status",
        "changed",
        "mutation_outcome",
        "outcome_finality",
        "error_code",
        "effect_id",
        "evidence_id",
        "workflow_id",
        "instance_id",
        "final_status",
    ):
        value = _scalar_value(payload.get(key))
        if value is not None:
            outcome[key] = value
    target_ids = payload.get("result_target_ids")
    if isinstance(target_ids, list):
        bounded_ids = [
            item
            for item in (_clean_text(value) for value in target_ids[:8])
            if item
        ]
        if bounded_ids:
            outcome["result_target_ids"] = bounded_ids
    return outcome


def _summary(
    *,
    capability_label: str,
    relation: Mapping[str, Any] | None,
    lifecycle_status: str,
    success: bool | None,
    result: Mapping[str, Any] | None,
) -> str:
    if isinstance(relation, Mapping):
        subject = str(relation["subject"].get("display") or "the subject")
        predicate = str(relation["predicate"].get("display") or "the relation")
        target = str(relation["object"].get("display") or "the object")
        target_is_text = relation["object"].get("value_kind") == "text"
        if target_is_text:
            target = f"“{target}”"
        target_role_label = "Value" if target_is_text else "Object"
        statement = (
            f"Subject: {subject}; Relation: {predicate}; "
            f"{target_role_label}: {target}"
        )
        if lifecycle_status == "running":
            return f"{capability_label}: {statement} (in progress)."
        outcome = result if isinstance(result, Mapping) else {}
        changed = outcome.get("changed")
        effect_status = str(outcome.get("effect_status") or "").strip().lower()
        mutation_outcome = str(
            outcome.get("mutation_outcome") or ""
        ).strip().lower()
        if success is True:
            if changed is False:
                return (
                    f"{capability_label} confirmed no change was needed: "
                    f"{statement}."
                )
            if changed is True:
                return f"{capability_label} reported a change: {statement}."
            return f"{capability_label} finished: {statement}."
        if success is False:
            if effect_status == "partial":
                return f"{capability_label} completed only partially: {statement}."
            if effect_status == "indeterminate" or mutation_outcome == "unknown":
                return (
                    f"The outcome of {capability_label} could not be verified: "
                    f"{statement}."
                )
            return f"{capability_label} did not succeed: {statement}."
        return f"{capability_label}: {statement}."
    if lifecycle_status == "running":
        return f"Using {capability_label}."
    if success is True:
        return f"Finished {capability_label}."
    if success is False:
        return f"{capability_label} did not succeed."
    return f"Observed {capability_label}."


def build_semantic_operation_projection(
    *,
    operation_id: str,
    capability_name: str,
    execution_method: str,
    capability_kind: str,
    arguments: Mapping[str, Any],
    lifecycle_status: str,
    success: bool | None = None,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one bounded lifecycle projection without additional retrieval."""

    operation_id_value = _clean_text(operation_id) or "unknown-operation"
    capability_id = _clean_text(capability_name) or "unknown_capability"
    execution_method_id = _clean_text(execution_method) or capability_id
    kind = _clean_text(capability_kind) or "registered_tool"
    status = _clean_text(lifecycle_status, limit=80) or "unknown"
    capability_label = _display_label(capability_id)
    projected_arguments = _argument_projection(arguments)
    relation = _relation_projection(projected_arguments)
    projection: dict[str, Any] = {
        "schema_version": SEMANTIC_OPERATION_SCHEMA_VERSION,
        "operation_id": operation_id_value,
        "lifecycle_status": status,
        "capability": {
            "id": capability_id,
            "label": capability_label,
            "kind": kind,
            "execution_method": execution_method_id,
        },
        "arguments": projected_arguments,
        "summary": _summary(
            capability_label=capability_label,
            relation=relation,
            lifecycle_status=status,
            success=success,
            result=result,
        ),
        "visibility": "actor_scope",
    }
    if relation is not None:
        projection["relation"] = relation
    outcome = _outcome_projection(
        result,
        lifecycle_status=status,
        success=success,
    )
    if outcome is not None:
        projection["outcome"] = outcome
    return projection


__all__ = [
    "SEMANTIC_OPERATION_SCHEMA_VERSION",
    "build_semantic_operation_projection",
]
