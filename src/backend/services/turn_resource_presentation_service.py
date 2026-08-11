"""Plan compact, trusted connector-resource annotations for turn screens."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .conversation_turn_memory_context_service import (
    presented_connector_resources_from_conversation_situation,
)

PRESENTED_CONNECTOR_RESOURCES_SCHEMA_VERSION = "presented_connector_resources.v1"
TURN_RESOURCE_PRESENTATION_PLAN_SCHEMA_VERSION = "turn_resource_presentation_plan.v1"
_SUCCESS_STATUSES = frozenset({"ok", "success", "succeeded", "completed"})
_DISPLAY_LABEL_MAX_CHARS = 320
_MAX_RESOURCES_PER_FAMILY = 8
_MAX_FAMILIES_PER_TURN = 4


def _clean_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    if not cleaned or len(cleaned) > maximum:
        return None
    return cleaned


def _successful_invocation(invocation: Mapping[str, Any]) -> bool:
    status = _clean_text(invocation.get("status"), maximum=40)
    if status:
        return status.casefold() in _SUCCESS_STATUSES
    evidence = invocation.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    evidence_status = _clean_text(evidence.get("status"), maximum=40)
    return bool(evidence_status and evidence_status.casefold() in _SUCCESS_STATUSES)


def _is_effect_invocation(invocation: Mapping[str, Any]) -> bool:
    return bool(
        _clean_text(invocation.get("effect_id"), maximum=512)
        or _clean_text(invocation.get("effect_status"), maximum=40)
        or _clean_text(invocation.get("mutation_outcome"), maximum=80)
        or isinstance(invocation.get("changed"), bool)
    )


def _family_display_name(source_family: str) -> str:
    words = source_family.replace("-", " ").replace("_", " ").split()
    return " ".join(word[:1].upper() + word[1:] for word in words)


def plan_turn_resource_presentation(
    *,
    tool_invocations: Sequence[Mapping[str, Any]] | None,
    prior_situation: str | None,
) -> dict[str, Any] | None:
    """Return first-use/change annotations and a server-authored memory capsule.

    Only the stable resource identity and human label attached by trusted tool
    binding are used.  The retained capsule suppresses repeated presentation;
    it is never consulted for authority or capability selection.
    """

    resources_by_family: dict[str, dict[str, dict[str, str]]] = {}
    family_order: list[str] = []
    for invocation in tool_invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        if not _successful_invocation(invocation) or _is_effect_invocation(invocation):
            continue
        resource_scope = invocation.get("resource_scope")
        if not isinstance(resource_scope, Mapping):
            continue
        source_family = _clean_text(resource_scope.get("source_family"), maximum=80)
        resource_id = _clean_text(resource_scope.get("resource_id"), maximum=512)
        display_label = _clean_text(
            resource_scope.get("display_label"),
            maximum=_DISPLAY_LABEL_MAX_CHARS,
        )
        if not source_family or not resource_id or not display_label:
            continue
        family_key = source_family.casefold()
        if family_key not in resources_by_family:
            if len(family_order) >= _MAX_FAMILIES_PER_TURN:
                continue
            resources_by_family[family_key] = {}
            family_order.append(family_key)
        family_resources = resources_by_family[family_key]
        if (
            resource_id.casefold() not in family_resources
            and len(family_resources) >= _MAX_RESOURCES_PER_FAMILY
        ):
            continue
        family_resources[resource_id.casefold()] = {
            "resource_id": resource_id,
            "display_label": display_label,
        }

    if not resources_by_family:
        return None

    prior_capsules = presented_connector_resources_from_conversation_situation(
        prior_situation
    )
    prior_resources_by_family: dict[str, frozenset[tuple[str, str]]] = {}
    for capsule in prior_capsules:
        source_family = _clean_text(capsule.get("source_family"), maximum=80)
        resources = capsule.get("resources")
        if not source_family or not isinstance(resources, Sequence):
            continue
        prior_resources_by_family[source_family.casefold()] = frozenset(
            (resource_id.casefold(), display_label.casefold())
            for resource in resources
            if isinstance(resource, Mapping)
            and (resource_id := _clean_text(resource.get("resource_id"), maximum=512))
            and (
                display_label := _clean_text(
                    resource.get("display_label"),
                    maximum=_DISPLAY_LABEL_MAX_CHARS,
                )
            )
        )

    annotations: list[dict[str, Any]] = []
    capsules: list[dict[str, Any]] = []
    for family_key in family_order:
        resources = list(resources_by_family[family_key].values())
        source_family = _clean_text(family_key, maximum=80)
        if not source_family or not resources:
            continue
        resource_identities = frozenset(
            (
                resource["resource_id"].casefold(),
                resource["display_label"].casefold(),
            )
            for resource in resources
        )
        if prior_resources_by_family.get(family_key) == resource_identities:
            continue
        annotations.append(
            {
                "source_family": source_family,
                "family_label": _family_display_name(source_family),
                "display_labels": [resource["display_label"] for resource in resources],
            }
        )
        capsules.append(
            {
                "schema_version": PRESENTED_CONNECTOR_RESOURCES_SCHEMA_VERSION,
                "source_family": source_family,
                "resources": resources,
            }
        )

    if not annotations:
        return None
    return {
        "schema_version": TURN_RESOURCE_PRESENTATION_PLAN_SCHEMA_VERSION,
        "annotations": annotations,
        "presentation_capsules": capsules,
    }


def annotate_turn_screen_text(
    screen_text: str,
    *,
    presentation_plan: Mapping[str, Any] | None,
) -> tuple[str, bool]:
    """Prepend compact resource labels without altering the spoken channel."""

    if not isinstance(screen_text, str) or not screen_text.strip():
        return screen_text, False
    if not isinstance(presentation_plan, Mapping):
        return screen_text, False
    annotations = presentation_plan.get("annotations")
    if not isinstance(annotations, Sequence) or isinstance(
        annotations, (str, bytes, bytearray)
    ):
        return screen_text, False
    lines: list[str] = []
    for annotation in annotations:
        if not isinstance(annotation, Mapping):
            continue
        family_label = _clean_text(annotation.get("family_label"), maximum=80)
        display_labels = annotation.get("display_labels")
        if not family_label or not isinstance(display_labels, Sequence):
            continue
        labels = [
            label
            for value in display_labels[:_MAX_RESOURCES_PER_FAMILY]
            if (label := _clean_text(value, maximum=_DISPLAY_LABEL_MAX_CHARS))
        ]
        if labels:
            lines.append(f"_{family_label}: {', '.join(labels)}_")
    if not lines:
        return screen_text, False
    return "\n".join(lines) + "\n\n" + screen_text.strip(), True


__all__ = [
    "PRESENTED_CONNECTOR_RESOURCES_SCHEMA_VERSION",
    "TURN_RESOURCE_PRESENTATION_PLAN_SCHEMA_VERSION",
    "annotate_turn_screen_text",
    "plan_turn_resource_presentation",
]
