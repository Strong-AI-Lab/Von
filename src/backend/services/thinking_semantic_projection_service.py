"""Bounded, human-readable projections of capability activity.

The projection is deliberately a view model, not a second execution record.
It retains exact identifiers needed for inspection while giving the ordinary
Thinking surface labels and role-labelled arguments that a person can read.
No database lookup is performed here: trusted display labels already resolved
by capability discovery may be carried in, with canonical identifiers retained
as the bounded fallback and inspection identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SEMANTIC_OPERATION_SCHEMA_VERSION = "thinking_semantic_operation.v1"
_MAX_TEXT_CHARS = 240
_MAX_SUMMARY_CHARS = 1_024
_MAX_ARGUMENTS = 8
_MAX_OBSERVATION_ITEMS = 5
_MAX_OBSERVATION_DETAILS = 4

_ARGUMENT_LABELS = {
    "subject": "Subject",
    "predicate": "Relation",
    "object": "Object",
    "value": "Value",
    "workflow": "Workflow",
    "instance": "Workflow instance",
    "name": "Name",
    "mailbox": "Mailbox",
    "message": "Message ID",
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
    ("profile", "mailbox", "Mailbox", "identifier"),
    ("profile_id", "mailbox", "Mailbox", "identifier"),
    ("message_id", "message", "Message ID", "identifier"),
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
_FOCUS_SPECS = (
    ("query", "query", "Query", "text"),
    ("instance_of", "type", "Type", "concept"),
    ("concept_id", "concept", "Concept", "concept"),
    ("profile", "mailbox", "Mailbox profile", "identifier"),
    ("profile_id", "mailbox", "Mailbox profile", "identifier"),
)
_FOCUS_SPEC_BY_ROLE = {
    role: (
        frozenset(
            source_argument
            for source_argument, source_role, _label, _value_kind in _FOCUS_SPECS
            if source_role == role
        ),
        label,
        value_kind,
    )
    for _source_argument, role, label, value_kind in _FOCUS_SPECS
}
_OBSERVATION_COUNT_LABELS = {
    "total_count": "concept matches",
    "total_predicates": "predicates",
    "messages": "messages",
}
_OBSERVATION_COUNT_SINGULAR_LABELS = {
    "concept matches": "concept match",
    "predicates": "predicate",
    "messages": "message",
}
_OBSERVATION_COLLECTIONS = ("results", "predicates", "messages")
_OBSERVATION_DETAIL_SPECS = {
    "authorised_email": ("Mailbox", "email"),
    "sender": ("Sender", "text"),
    "date": ("Date", "text"),
    "last_message_at": ("Date", "datetime"),
    "token_status": ("Authorisation", "status"),
}
_OBSERVATION_IDENTIFIER_KINDS = frozenset({"predicate", "message"})
_BOUNDED_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:@+-]+")


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
    cleaned = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", cleaned)
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


def _capability_display_label(
    *,
    capability_id: str,
    capability_kind: str,
    projected_arguments: list[dict[str, Any]],
    display_name: Any = None,
    workflow_identity: Any = None,
) -> str:
    """Choose a bounded human label without replacing stable capability identity."""

    candidate = _clean_text(display_name)
    generated_from_capability_id = _display_label(capability_id)
    if candidate and not (
        capability_kind == "represented_workflow"
        and candidate == generated_from_capability_id
    ):
        return _display_label(candidate) if candidate.startswith("#V#") else candidate

    if capability_kind == "represented_workflow":
        workflow = _argument_by_role(projected_arguments, "workflow")
        argument_workflow_identity = _clean_text(
            workflow.get("value") if isinstance(workflow, Mapping) else None
        )
        if argument_workflow_identity:
            return _display_label(argument_workflow_identity)

        outcome_workflow_identity = _clean_text(workflow_identity)
        if outcome_workflow_identity:
            return _display_label(outcome_workflow_identity)

    return generated_from_capability_id


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


def _focus_projection(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project the primary object or query of a non-relation operation."""

    for source_argument, role, label, value_kind in _FOCUS_SPECS:
        value = _argument_value(arguments.get(source_argument))
        if value is None:
            continue
        display = (
            value
            if isinstance(value, str) and value_kind == "text"
            else _display_label(value)
            if isinstance(value, str)
            else str(value)
        )
        focus: dict[str, Any] = {
            "role": role,
            "label": label,
            "source_argument": source_argument,
            "value_kind": value_kind,
            "value": value,
            "display": display,
        }
        if isinstance(value, str) and value.startswith("#V#"):
            focus["concept_id"] = value
        return focus
    return None


def _observation_item(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None

    containers = [value]
    for preview_key in ("concept_preview", "predicate_preview"):
        preview = value.get(preview_key)
        if isinstance(preview, Mapping):
            containers.append(preview)

    name = next(
        (
            item
            for item in (
                _clean_text(container.get(key), limit=120)
                for container in containers
                for key in (
                    "name",
                    "label",
                    "title",
                    "display_name",
                    "subject",
                    "session_name",
                    "authorised_email",
                )
            )
            if item
        ),
        None,
    )
    identifier = None
    identifier_kind = None
    for container in containers:
        for key, kind in (
            ("concept_id", "concept"),
            ("predicate_concept_id", "predicate"),
            ("message_id", "message"),
        ):
            candidate = _clean_text(container.get(key), limit=160)
            if not candidate:
                continue
            if kind == "concept" and not candidate.startswith("#V#"):
                continue
            if (
                kind != "concept"
                and not candidate.startswith("#V#")
                and not _BOUNDED_IDENTIFIER_RE.fullmatch(candidate)
            ):
                continue
            identifier = candidate
            identifier_kind = kind
            break
        if identifier is not None:
            break
    if name is None and identifier is not None and identifier_kind != "message":
        name = _display_label(identifier)
    if name is None:
        return None

    item = {"name": name}
    if identifier is not None:
        item["identifier"] = identifier
        if (
            identifier_kind in _OBSERVATION_IDENTIFIER_KINDS
            and not identifier.startswith("#V#")
        ):
            item["identifier_kind"] = identifier_kind
    return item


def _observation_details(result: Mapping[str, Any]) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for source_field, (label, value_kind) in _OBSERVATION_DETAIL_SPECS.items():
        value = _clean_text(result.get(source_field), limit=160)
        if not value:
            continue
        details.append(
            {
                "label": label,
                "source_field": source_field,
                "value_kind": value_kind,
                "value": value,
            }
        )
        if len(details) >= _MAX_OBSERVATION_DETAILS:
            break
    return details


def _observation_projection(
    result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Project a bounded result from common read-tool response shapes."""

    if not isinstance(result, Mapping):
        return None

    count_source = next(
        (
            key
            for key in _OBSERVATION_COUNT_LABELS
            if isinstance(result.get(key), (int, float))
            and not isinstance(result.get(key), bool)
        ),
        None,
    )
    count = max(0, int(result[count_source])) if count_source is not None else None

    collection_source = next(
        (key for key in _OBSERVATION_COLLECTIONS if isinstance(result.get(key), list)),
        None,
    )
    raw_items = result.get(collection_source) if collection_source else None
    if (
        count is None
        and collection_source == "messages"
        and isinstance(raw_items, list)
    ):
        count_source = "messages"
        count = len(raw_items)
    items = [
        item
        for item in (
            _observation_item(value)
            for value in (
                raw_items[:_MAX_OBSERVATION_ITEMS]
                if isinstance(raw_items, list)
                else []
            )
        )
        if item is not None
    ]

    if (
        not items
        and collection_source is None
        and result.get("success") is not False
        and not _clean_text(result.get("error_code"), limit=80)
    ):
        root_item = _observation_item(result)
        if root_item is not None:
            items.append(root_item)

    details = _observation_details(result)
    if count is None and not items and not details:
        return None

    observation: dict[str, Any] = {
        "items": items,
        "count_is_lower_bound": bool(
            result.get("counts_are_lower_bounds") is True
            or result.get("total_count_is_lower_bound") is True
            or result.get("coverage_complete") is False
        ),
    }
    if count is not None and count_source is not None:
        observation.update(
            {
                "count": count,
                "count_label": _OBSERVATION_COUNT_LABELS[count_source],
                "count_source": count_source,
            }
        )
    if collection_source is not None:
        observation["collection_source"] = collection_source
    if details:
        observation["details"] = details
    return observation


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


def _focus_statement(focus: Mapping[str, Any] | None) -> str | None:
    if not isinstance(focus, Mapping):
        return None
    label = _clean_text(focus.get("label"), limit=40) or "Target"
    display = _clean_text(focus.get("display"))
    if not display:
        return None
    if focus.get("value_kind") == "text":
        display = f"“{display}”"
    return f"{label}: {display}"


def _join_observation_names(names: list[str], omitted_count: int) -> str:
    parts = list(names)
    if omitted_count > 0:
        parts.append(f"{omitted_count} other{'s' if omitted_count != 1 else ''}")
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _observation_statement(observation: Mapping[str, Any] | None) -> str | None:
    if not isinstance(observation, Mapping):
        return None
    count_value = observation.get("count")
    count = (
        max(0, int(count_value))
        if isinstance(count_value, (int, float)) and not isinstance(count_value, bool)
        else None
    )
    count_label = _clean_text(observation.get("count_label"), limit=80)
    raw_items = observation.get("items")
    names = [
        name
        for name in (
            _clean_text(item.get("name"), limit=120)
            for item in (
                raw_items[:_MAX_OBSERVATION_ITEMS]
                if isinstance(raw_items, list)
                else []
            )
            if isinstance(item, Mapping)
        )
        if name
    ]
    if count is not None and count_label:
        rendered_count_label = (
            _OBSERVATION_COUNT_SINGULAR_LABELS.get(count_label, count_label)
            if count == 1
            else count_label
        )
        if count == 0:
            if observation.get("count_is_lower_bound") is True:
                return f"0 {count_label} (coverage incomplete)"
            return f"no {count_label}"
        qualifier = (
            "at least " if observation.get("count_is_lower_bound") is True else ""
        )
        statement = f"{qualifier}{count} {rendered_count_label}"
        if names:
            omitted_count = max(0, count - len(names))
            statement += f": {_join_observation_names(names, omitted_count)}"
        return statement
    if names:
        return _join_observation_names(names, 0)
    return None


def _observation_details_statement(
    observation: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(observation, Mapping):
        return None
    raw_details = observation.get("details")
    if not isinstance(raw_details, list):
        return None
    item_names = {
        name.casefold()
        for name in (
            _clean_text(item.get("name"), limit=120)
            for item in observation.get("items", [])
            if isinstance(item, Mapping)
        )
        if name
    }
    parts: list[str] = []
    for detail in raw_details[:_MAX_OBSERVATION_DETAILS]:
        if not isinstance(detail, Mapping):
            continue
        label = _clean_text(detail.get("label"), limit=60)
        value = _clean_text(detail.get("value"), limit=160)
        if not label or not value or value.casefold() in item_names:
            continue
        parts.append(f"{label}: {value}")
    return "; ".join(parts) if parts else None


def _append_observation_details(statement: str, details: str | None) -> str:
    if not details:
        return statement
    return f"{statement.rstrip('.')} ({details})."


def _summary(
    *,
    capability_label: str,
    relation: Mapping[str, Any] | None,
    focus: Mapping[str, Any] | None,
    observation: Mapping[str, Any] | None,
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
            f"Subject: {subject}; Relation: {predicate}; {target_role_label}: {target}"
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
                        f"{capability_label} verified the reported change: {statement}."
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

    focus_text = _focus_statement(focus)
    observation_text = _observation_statement(observation)
    observation_details = _observation_details_statement(observation)
    if lifecycle_status == "running":
        if focus_text:
            return f"{capability_label}: {focus_text} (in progress)."
        return f"Using {capability_label}."
    if success is True:
        if observation_text and focus_text:
            return _append_observation_details(
                f"{capability_label} returned {observation_text} for {focus_text}.",
                observation_details,
            )
        if observation_text:
            return _append_observation_details(
                f"{capability_label} returned {observation_text}.",
                observation_details,
            )
        if observation_details and focus_text:
            return (
                f"{capability_label} finished for {focus_text} ({observation_details})."
            )
        if observation_details:
            return f"{capability_label} finished ({observation_details})."
        if focus_text:
            return (
                f"{capability_label} finished for {focus_text}; "
                "no bounded result summary was available."
            )
        return f"Finished {capability_label}."
    if success is False:
        if focus_text:
            return f"{capability_label} did not succeed for {focus_text}."
        return f"{capability_label} did not succeed."
    if focus_text:
        return f"{capability_label}: {focus_text}."
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


def _normalise_projected_focus(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    role = _clean_text(value.get("role"), limit=40)
    if role not in _FOCUS_SPEC_BY_ROLE:
        return None
    expected_sources, label, expected_kind = _FOCUS_SPEC_BY_ROLE[role]
    source_argument = _clean_text(value.get("source_argument"), limit=80)
    value_kind = _clean_text(value.get("value_kind"), limit=40)
    if source_argument not in expected_sources or value_kind != expected_kind:
        return None
    focus_value = _argument_value(value.get("value"))
    if focus_value is None:
        return None
    display = (
        focus_value
        if isinstance(focus_value, str) and value_kind == "text"
        else (
            _display_label(focus_value)
            if isinstance(focus_value, str)
            else str(focus_value)
        )
    )
    focus: dict[str, Any] = {
        "role": role,
        "label": label,
        "source_argument": source_argument,
        "value_kind": value_kind,
        "value": focus_value,
        "display": display,
    }
    if isinstance(focus_value, str) and focus_value.startswith("#V#"):
        focus["concept_id"] = focus_value
    return focus


def _normalise_projected_observation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    count_source = _clean_text(value.get("count_source"), limit=80)
    count_value = value.get("count")
    count = (
        max(0, int(count_value))
        if count_source in _OBSERVATION_COUNT_LABELS
        and isinstance(count_value, (int, float))
        and not isinstance(count_value, bool)
        else None
    )
    collection_source = _clean_text(value.get("collection_source"), limit=80)
    if collection_source not in _OBSERVATION_COLLECTIONS:
        collection_source = None

    raw_items = value.get("items")
    items: list[dict[str, str]] = []
    if isinstance(raw_items, list):
        for raw_item in raw_items[:_MAX_OBSERVATION_ITEMS]:
            if not isinstance(raw_item, Mapping):
                continue
            name = _clean_text(raw_item.get("name"), limit=120)
            if not name:
                continue
            item = {"name": name}
            identifier = _clean_text(raw_item.get("identifier"), limit=160)
            identifier_kind = _clean_text(raw_item.get("identifier_kind"), limit=40)
            if identifier and identifier.startswith("#V#"):
                item["identifier"] = identifier
            elif (
                identifier
                and identifier_kind in _OBSERVATION_IDENTIFIER_KINDS
                and _BOUNDED_IDENTIFIER_RE.fullmatch(identifier)
            ):
                item["identifier"] = identifier
                item["identifier_kind"] = identifier_kind
            items.append(item)

    details: list[dict[str, str]] = []
    raw_details = value.get("details")
    if isinstance(raw_details, list):
        for raw_detail in raw_details[:_MAX_OBSERVATION_DETAILS]:
            if not isinstance(raw_detail, Mapping):
                continue
            source_field = _clean_text(raw_detail.get("source_field"), limit=80)
            spec = _OBSERVATION_DETAIL_SPECS.get(source_field or "")
            if spec is None:
                continue
            label, value_kind = spec
            detail_value = _clean_text(raw_detail.get("value"), limit=160)
            if not detail_value:
                continue
            details.append(
                {
                    "label": label,
                    "source_field": source_field,
                    "value_kind": value_kind,
                    "value": detail_value,
                }
            )

    if count is None and not items and not details:
        return None
    observation: dict[str, Any] = {
        "items": items,
        "count_is_lower_bound": value.get("count_is_lower_bound") is True,
    }
    if count is not None and count_source is not None:
        observation.update(
            {
                "count": count,
                "count_label": _OBSERVATION_COUNT_LABELS[count_source],
                "count_source": count_source,
            }
        )
    if collection_source is not None:
        observation["collection_source"] = collection_source
    if details:
        observation["details"] = details
    return observation


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
    outcome = _normalise_projected_outcome(value.get("outcome"))
    capability_label = _capability_display_label(
        capability_id=capability_id,
        capability_kind=kind,
        projected_arguments=arguments,
        display_name=raw_capability.get("label"),
        workflow_identity=(outcome or {}).get("workflow_id"),
    )
    relation = _relation_projection(arguments)
    focus = _normalise_projected_focus(value.get("focus")) if relation is None else None
    observation = (
        _normalise_projected_observation(value.get("observation"))
        if relation is None
        else None
    )
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
            "label": capability_label,
            "kind": kind,
            "execution_method": execution_method,
        },
        "arguments": arguments,
        "summary": _clean_text(
            _summary(
                capability_label=capability_label,
                relation=relation,
                focus=focus,
                observation=observation,
                lifecycle_status=lifecycle_status,
                success=success if isinstance(success, bool) else None,
                result=outcome,
                verification=verification,
            ),
            limit=_MAX_SUMMARY_CHARS,
        )
        or f"Observed {capability_label}.",
        "visibility": "conversation_scope",
        "verification": verification,
    }
    if relation is not None:
        projection["relation"] = relation
    if focus is not None:
        projection["focus"] = focus
    if observation is not None:
        projection["observation"] = observation
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
    capability_display_name: str | None = None,
    success: bool | None = None,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one bounded lifecycle projection without additional retrieval."""

    operation_id_value = _clean_text(operation_id) or "unknown-operation"
    capability_id = _clean_text(capability_name) or "unknown_capability"
    execution_method_id = _clean_text(execution_method) or capability_id
    kind = _clean_text(capability_kind) or "registered_tool"
    status = _clean_text(lifecycle_status, limit=80) or "unknown"
    projected_arguments = _argument_projection(arguments)
    capability_label = _capability_display_label(
        capability_id=capability_id,
        capability_kind=kind,
        projected_arguments=projected_arguments,
        display_name=capability_display_name,
        workflow_identity=(
            result.get("workflow_id")
            if isinstance(result, Mapping)
            else None
        ),
    )
    relation = _relation_projection(projected_arguments)
    focus = _focus_projection(arguments) if relation is None else None
    observation = _observation_projection(result) if relation is None else None
    verification = _verification_projection(
        result,
        lifecycle_status=status,
        relation=relation,
    )
    summary = _summary(
        capability_label=capability_label,
        relation=relation,
        focus=focus,
        observation=observation,
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
    if focus is not None:
        projection["focus"] = focus
    if observation is not None:
        projection["observation"] = observation
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
