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
_MAX_SUMMARY_CHARS = 1_024
_MAX_ARGUMENTS = 8

_ARGUMENT_LABELS = {
    "subject": "Subject",
    "predicate": "Relation",
    "object": "Object",
    "value": "Value",
    "workflow": "Workflow",
    "instance": "Workflow instance",
    "name": "Name",
}
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
_ARGUMENT_KIND_BY_ROLE = {
    role: value_kind for _key, role, _label, value_kind in _ARGUMENT_SPECS
}
_ARGUMENT_SOURCE_KEYS_BY_ROLE = {
    role: frozenset(
        key
        for key, source_role, _label, _value_kind in _ARGUMENT_SPECS
        if source_role == role
    )
    for _key, role, _label, _value_kind in _ARGUMENT_SPECS
}


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
            else _display_label(value) if isinstance(value, str) else str(value)
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
            item for item in (_clean_text(value) for value in target_ids[:8]) if item
        ]
        if bounded_ids:
            outcome["result_target_ids"] = bounded_ids
    return outcome


def _read_back_value(value: Any) -> str | int | float | bool | None:
    scalar = _scalar_value(value)
    if scalar is not None:
        return scalar
    if isinstance(value, Mapping):
        for key in ("concept_id", "predicate_id", "id", "value", "text"):
            scalar = _scalar_value(value.get(key))
            if scalar is not None:
                return scalar
    return None


def _read_back_relation_assessment(
    read_back: Mapping[str, Any],
    relation: Mapping[str, Any] | None,
) -> tuple[bool, bool]:
    """Return whether a relation proof exists and whether it matches the view."""

    relation_keys = frozenset(
        {
            "subject_concept_id",
            "source_id",
            "concept_id",
            "subject",
            "predicate",
            "predicate_id",
            "predicate_ref",
            "target_concept_id",
            "target_id",
            "target",
            "object_concept_id",
            "object",
            "target_text",
            "text",
            "object_text",
        }
    )
    if not relation_keys.intersection(read_back):
        return False, True
    if not isinstance(relation, Mapping):
        return True, True

    def first_value(*keys: str) -> str | int | float | bool | None:
        for key in keys:
            if key not in read_back:
                continue
            value = _read_back_value(read_back.get(key))
            if value is not None:
                return value
        return None

    actual_subject = first_value(
        "subject_concept_id",
        "source_id",
        "concept_id",
        "subject",
    )
    actual_predicate = first_value("predicate", "predicate_id", "predicate_ref")
    actual_object = first_value(
        "target_concept_id",
        "target_id",
        "target",
        "object_concept_id",
        "object",
        "target_text",
        "text",
        "object_text",
    )
    expected_subject = _read_back_value(relation.get("subject"))
    expected_predicate = _read_back_value(relation.get("predicate"))
    expected_object = _read_back_value(relation.get("object"))
    matches = (
        actual_subject is not None
        and actual_predicate is not None
        and actual_object is not None
        and actual_subject == expected_subject
        and actual_predicate == expected_predicate
        and actual_object == expected_object
    )
    return True, matches


def _verification_projection(
    result: Mapping[str, Any] | None,
    *,
    lifecycle_status: str,
    relation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Describe the evidence boundary without performing another read.

    A tool result is an effect receipt, not canonical verification.  Only an
    explicit, successful ``canonical_read_back`` evidence upgrades the
    projection to ``verified``.  A non-empty object alone is insufficient and a
    relation-shaped read-back must match the relation being displayed.  The
    read-back body is deliberately not copied into live progress: the Thinking
    view needs the evidence class, not an unbounded or potentially sensitive
    second result payload.
    """

    payload = result if isinstance(result, Mapping) else {}
    raw_read_back = payload.get("canonical_read_back")
    read_back_present = isinstance(raw_read_back, Mapping)
    successful_receipt = (
        payload.get("success") is True
        and str(payload.get("effect_status") or "").strip().lower() == "succeeded"
        and not _clean_text(payload.get("error"))
        and not _clean_text(payload.get("error_code"))
    )
    read_back_reports_failure = bool(
        isinstance(raw_read_back, Mapping)
        and (
            raw_read_back.get("success") is False
            or _clean_text(raw_read_back.get("error"))
            or _clean_text(raw_read_back.get("error_code"))
        )
    )
    has_relation_proof = False
    relation_matches = True
    explicit_read_back_success = False
    if isinstance(raw_read_back, Mapping):
        has_relation_proof, relation_matches = _read_back_relation_assessment(
            raw_read_back,
            relation,
        )
        explicit_read_back_success = (
            bool(has_relation_proof and relation_matches)
            if isinstance(relation, Mapping)
            else raw_read_back.get("success") is True
        )
    read_back_verified = bool(
        read_back_present
        and raw_read_back
        and successful_receipt
        and explicit_read_back_success
        and not read_back_reports_failure
        and relation_matches
    )
    if read_back_verified:
        status = "verified"
        source = "canonical_read_back"
    elif result is not None or lifecycle_status != "running":
        status = "receipt_only"
        source = "effect_receipt"
    else:
        status = "unknown"
        source = "none"
    return {
        "status": status,
        "canonical_read_back_present": read_back_present,
        "source": source,
    }


def _summary(
    *,
    capability_label: str,
    relation: Mapping[str, Any] | None,
    lifecycle_status: str,
    success: bool | None,
    result: Mapping[str, Any] | None,
    verification: Mapping[str, Any],
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
        mutation_outcome = str(outcome.get("mutation_outcome") or "").strip().lower()
        read_back_verified = verification.get("status") == "verified"
        if success is True:
            if changed is False:
                if read_back_verified:
                    return (
                        f"{capability_label} verified that no change was needed: "
                        f"{statement}."
                    )
                return (
                    f"{capability_label} reported that no change was needed: "
                    f"{statement}."
                )
            if changed is True:
                if read_back_verified:
                    return (
                        f"{capability_label} verified the reported change: "
                        f"{statement}."
                    )
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


def _normalise_projected_arguments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    projected: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for raw_item in value[:_MAX_ARGUMENTS]:
        if not isinstance(raw_item, Mapping):
            continue
        role = _clean_text(raw_item.get("role"), limit=40)
        value_kind = _clean_text(raw_item.get("value_kind"), limit=40)
        if (
            role not in _ARGUMENT_LABELS
            or role in seen_roles
            or value_kind != _ARGUMENT_KIND_BY_ROLE.get(role)
        ):
            continue
        argument_value = _scalar_value(raw_item.get("value"))
        if argument_value is None:
            continue
        source_argument = _clean_text(
            raw_item.get("source_argument"),
            limit=80,
        )
        if source_argument not in _ARGUMENT_SOURCE_KEYS_BY_ROLE[role]:
            source_argument = "unknown"
        display = (
            argument_value
            if isinstance(argument_value, str) and value_kind == "text"
            else (
                _display_label(argument_value)
                if isinstance(argument_value, str)
                else str(argument_value)
            )
        )
        item: dict[str, Any] = {
            "role": role,
            "label": _ARGUMENT_LABELS[role],
            "source_argument": source_argument or "unknown",
            "value_kind": value_kind,
            "value": argument_value,
            "display": display,
        }
        if isinstance(argument_value, str) and argument_value.startswith("#V#"):
            item["concept_id"] = argument_value
        projected.append(item)
        seen_roles.add(role)
    return projected


def _normalise_projected_outcome(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    status = _clean_text(value.get("status"), limit=80)
    success = value.get("success")
    outcome: dict[str, Any] = {
        "status": status or "unknown",
        "success": success if isinstance(success, bool) else None,
    }
    for key in (
        "effect_status",
        "changed",
        "mutation_outcome",
        "outcome_finality",
        "error_code",
        "workflow_id",
        "instance_id",
        "final_status",
    ):
        item = _scalar_value(value.get(key))
        if item is not None:
            outcome[key] = item
    target_ids = value.get("result_target_ids")
    if isinstance(target_ids, list):
        bounded_ids = [
            item
            for item in (_clean_text(raw) for raw in target_ids[:_MAX_ARGUMENTS])
            if item
        ]
        if bounded_ids:
            outcome["result_target_ids"] = bounded_ids
    return outcome


def _normalise_projected_verification(
    value: Any,
    *,
    lifecycle_status: str,
    has_outcome: bool,
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        status = _clean_text(value.get("status"), limit=40)
        source = _clean_text(value.get("source"), limit=40)
        present = value.get("canonical_read_back_present") is True
        has_terminal_evidence = has_outcome or lifecycle_status != "running"
        if (
            has_terminal_evidence
            and status == "verified"
            and present
            and source == "canonical_read_back"
        ):
            return {
                "status": "verified",
                "canonical_read_back_present": True,
                "source": "canonical_read_back",
            }
        if (
            has_terminal_evidence
            and status == "receipt_only"
            and source == "effect_receipt"
        ):
            return {
                "status": "receipt_only",
                "canonical_read_back_present": present,
                "source": "effect_receipt",
            }
        if (
            not has_terminal_evidence
            and status == "unknown"
            and source == "none"
            and not present
        ):
            return {
                "status": "unknown",
                "canonical_read_back_present": False,
                "source": "none",
            }
    if has_outcome or lifecycle_status != "running":
        return {
            "status": "receipt_only",
            "canonical_read_back_present": False,
            "source": "effect_receipt",
        }
    return {
        "status": "unknown",
        "canonical_read_back_present": False,
        "source": "none",
    }


def normalise_semantic_operation_projection(value: Any) -> dict[str, Any] | None:
    """Return a strictly bounded v1 projection safe for live/reloaded display.

    Stored progress is treated as untrusted display data.  This function keeps
    only the v1 allowlist, bounds every collection and string, reconstructs the
    relation from role-labelled arguments, and accepts only the explicit
    conversation-participant visibility contract.
    """

    if not isinstance(value, Mapping):
        return None
    if value.get("schema_version") != SEMANTIC_OPERATION_SCHEMA_VERSION:
        return None
    if value.get("visibility") != "conversation_scope":
        return None
    operation_id = _clean_text(value.get("operation_id"))
    lifecycle_status = _clean_text(value.get("lifecycle_status"), limit=80)
    raw_capability = value.get("capability")
    if (
        not operation_id
        or not lifecycle_status
        or not isinstance(raw_capability, Mapping)
    ):
        return None
    capability_id = _clean_text(raw_capability.get("id"))
    if not capability_id:
        return None
    kind = _clean_text(raw_capability.get("kind")) or "registered_tool"
    execution_method = (
        _clean_text(raw_capability.get("execution_method")) or capability_id
    )
    arguments = _normalise_projected_arguments(value.get("arguments"))
    relation = _relation_projection(arguments)
    outcome = _normalise_projected_outcome(value.get("outcome"))
    verification = _normalise_projected_verification(
        value.get("verification"),
        lifecycle_status=lifecycle_status,
        has_outcome=outcome is not None,
    )
    success = outcome.get("success") if outcome is not None else None
    projection: dict[str, Any] = {
        "schema_version": SEMANTIC_OPERATION_SCHEMA_VERSION,
        "operation_id": operation_id,
        "lifecycle_status": lifecycle_status,
        "capability": {
            "id": capability_id,
            "label": _display_label(capability_id),
            "kind": kind,
            "execution_method": execution_method,
        },
        "arguments": arguments,
        "summary": _clean_text(
            _summary(
                capability_label=_display_label(capability_id),
                relation=relation,
                lifecycle_status=lifecycle_status,
                success=success if isinstance(success, bool) else None,
                result=outcome,
                verification=verification,
            ),
            limit=_MAX_SUMMARY_CHARS,
        )
        or f"Observed {_display_label(capability_id)}.",
        "visibility": "conversation_scope",
        "verification": verification,
    }
    if relation is not None:
        projection["relation"] = relation
    if outcome is not None:
        projection["outcome"] = outcome
    return projection


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
    verification = _verification_projection(
        result,
        lifecycle_status=status,
        relation=relation,
    )
    summary = _summary(
        capability_label=capability_label,
        relation=relation,
        lifecycle_status=status,
        success=success,
        result=result,
        verification=verification,
    )
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
        "summary": _clean_text(summary, limit=_MAX_SUMMARY_CHARS)
        or f"Observed {capability_label}.",
        "visibility": "conversation_scope",
        "verification": verification,
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
    "normalise_semantic_operation_projection",
]
