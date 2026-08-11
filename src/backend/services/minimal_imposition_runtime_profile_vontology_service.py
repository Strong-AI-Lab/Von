"""Vontology-backed runtime profiles for minimal-imposition write-policy control.

This profile is the runtime counterpart to the benchmark profile introduced for
Minimal Imposition evaluation. It lets the write-policy workflow resolve a
stable, inspectable decision model from Vontology rather than treating the
runtime branch policy as an opaque Python-only heuristic.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from ..workflows.definitions import WRITE_TOOL_POLICY_WORKFLOW_ID

DEFAULT_MINIMAL_IMPOSITION_RUNTIME_PROFILE_PREDICATE = (
    "#V#has_minimal_imposition_runtime_profile_json"
)
DEFAULT_MINIMAL_IMPOSITION_RUNTIME_PROFILE_CONCEPT_ID = (
    "#V#minimal_imposition_runtime_profile_write_policy_v1"
)
MINIMAL_IMPOSITION_RUNTIME_PROFILE_TYPE_ID = "#V#minimal_imposition_runtime_profile"
MINIMAL_IMPOSITION_RUNTIME_PROFILE_LINK_PREDICATE = (
    "#V#has_minimal_imposition_runtime_profile"
)
DEFAULT_PROFILE_WORKFLOW_IDS: tuple[str, ...] = (WRITE_TOOL_POLICY_WORKFLOW_ID,)

_PROFILE_TEXT_PREDICATES: tuple[str, ...] = (
    "#V#has_minimal_imposition_runtime_profile_json",
    "has_minimal_imposition_runtime_profile_json",
    "hasMinimalImpositionRuntimeProfileJson",
    "hasContent",
)
_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    "#V#has_minimal_imposition_runtime_profile",
    "has_minimal_imposition_runtime_profile",
    "hasMinimalImpositionRuntimeProfile",
)

_DEFAULT_DECISION_POLICY: dict[str, Any] = {
    "policy_version": "minimal_imposition_runtime.v1",
    "default_allow_additive_low_risk": True,
    "require_clear_request_for_recoverable_mutation": True,
    "allow_recent_request_context_for_recoverable_mutation": True,
    "require_confirmation_for_destructive": True,
    "allow_recent_confirmation_context_for_destructive": True,
    "require_explicit_request_for_external": True,
    "allow_recent_request_context_for_external": True,
    "process_sensitive_additive_requires_clear_request": True,
    "prefer_narrowest_human_intervention": True,
    "missing_profile_strategy": "fallback_with_diagnostics",
}

_VALID_WRITE_RISK_CLASSES = frozenset(
    {
        "additive_low_risk",
        "mutative_non_destructive",
        "destructive",
        "external_non_vontology",
    }
)

_DEFAULT_TOOL_RISK_CLASSES: dict[str, str] = {
    "add_issue_comment": "external_non_vontology",
    "add_names": "additive_low_risk",
    "add_relationship": "additive_low_risk",
    "assign_copilot_to_issue": "external_non_vontology",
    "assign_task": "additive_low_risk",
    "create_branch": "external_non_vontology",
    "create_concepts": "additive_low_risk",
    "create_or_update_file": "external_non_vontology",
    "create_pull_request": "external_non_vontology",
    "create_repository": "external_non_vontology",
    "create_task": "additive_low_risk",
    "delete_concept": "destructive",
    "delete_file": "external_non_vontology",
    "delete_text_relation": "destructive",
    "download_paper": "additive_low_risk",
    "finalise_cached_paper": "additive_low_risk",
    "gmail_create_label": "external_non_vontology",
    "gmail_import_attachment": "additive_low_risk",
    "gmail_modify_labels": "external_non_vontology",
    "gmail_send_message": "external_non_vontology",
    "import_url_file_copy": "additive_low_risk",
    "issue_write": "external_non_vontology",
    "jira_add_attachment": "external_non_vontology",
    "jira_add_comment": "external_non_vontology",
    "jira_create_issue": "external_non_vontology",
    "jira_link_issue": "external_non_vontology",
    "jira_move_issue": "external_non_vontology",
    "jira_transition": "external_non_vontology",
    "jira_update_issue": "external_non_vontology",
    "materialise_scholarly_representation_for_file_copy": "additive_low_risk",
    "mcp__github__add_issue_comment": "external_non_vontology",
    "mcp__github__create_pull_request": "external_non_vontology",
    "mcp__github__update_pull_request": "external_non_vontology",
    "merge_concepts": "destructive",
    "merge_pull_request": "external_non_vontology",
    "pull_request_review_write": "external_non_vontology",
    "push_files": "external_non_vontology",
    "record_source_processing_marker": "additive_low_risk",
    "remove_relationship": "destructive",
    "remove_relationships_bulk": "destructive",
    "sub_issue_write": "external_non_vontology",
    "task_add_attachment": "additive_low_risk",
    "task_add_comment": "additive_low_risk",
    "task_add_worklog": "additive_low_risk",
    "task_assign": "mutative_non_destructive",
    "task_bulk_update": "mutative_non_destructive",
    "task_create": "additive_low_risk",
    "task_create_subtask": "additive_low_risk",
    "task_delete": "destructive",
    "task_import_jira_issues": "external_non_vontology",
    "task_link": "additive_low_risk",
    "task_set_parent": "mutative_non_destructive",
    "task_transition": "mutative_non_destructive",
    "task_unassign": "mutative_non_destructive",
    "task_unlink": "destructive",
    "task_update_fields": "mutative_non_destructive",
    "task_update_status": "mutative_non_destructive",
    "undo_relationship_removal": "mutative_non_destructive",
    "update_concept": "mutative_non_destructive",
    "update_pull_request": "external_non_vontology",
    "update_pull_request_branch": "external_non_vontology",
    "update_task_status": "mutative_non_destructive",
    "update_text_relation": "mutative_non_destructive",
    "upsert_renderer_profile": "additive_low_risk",
    "upsert_singleton_text_relation": "additive_low_risk",
    "upsert_text_relation": "additive_low_risk",
    "workflow_bind_event": "additive_low_risk",
    "workflow_cancel_instance": "destructive",
    "workflow_create_instance": "additive_low_risk",
    "workflow_create_schedule": "additive_low_risk",
    "workflow_delete_event_binding": "destructive",
    "workflow_delete_schedule": "destructive",
    "workflow_resume_instance": "mutative_non_destructive",
    "workflow_retry_instance": "mutative_non_destructive",
    "workflow_set_event_binding_enabled": "mutative_non_destructive",
    "workflow_set_schedule_enabled": "mutative_non_destructive",
    "workflow_trigger_schedule": "additive_low_risk",
}

_CANONICAL_MINIMAL_IMPOSITION_RUNTIME_PROFILE_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "profile_concept_id": DEFAULT_MINIMAL_IMPOSITION_RUNTIME_PROFILE_CONCEPT_ID,
        "profile_id": "write_policy_runtime_v1",
        "description": (
            "Canonical minimal-imposition runtime profile for the write-tool "
            "policy workflow. It makes human interruption a context-sensitive "
            "fallback: low-risk additive KB work may proceed by default, "
            "recoverable mutations require clear request evidence, destructive "
            "mutations require explicit confirmation, and process-sensitive or "
            "external side-effects remain guarded."
        ),
        "decision_policy": dict(_DEFAULT_DECISION_POLICY),
        "tool_risk_classes": dict(_DEFAULT_TOOL_RISK_CLASSES),
        "scenario_policies": [
            {
                "scenario_id": "low_risk_additive_internal_write",
                "desired_branch": "allow_without_interruption",
                "risk_classes": ["additive_low_risk"],
            },
            {
                "scenario_id": "reversible_update",
                "desired_branch": "allow_when_request_evidence_is_clear",
                "risk_classes": ["mutative_non_destructive"],
            },
            {
                "scenario_id": "destructive_action",
                "desired_branch": "explicit_confirmation_or_block",
                "risk_classes": ["destructive"],
            },
            {
                "scenario_id": "high_fan_out_change",
                "desired_branch": "guarded_request_or_confirmation",
                "tool_names": [
                    "merge_concepts",
                    "remove_relationships_bulk",
                    "workflow_bind_event",
                    "workflow_create_schedule",
                    "workflow_set_event_binding_enabled",
                    "workflow_set_schedule_enabled",
                    "workflow_delete_event_binding",
                    "workflow_delete_schedule",
                ],
            },
            {
                "scenario_id": "external_system_write",
                "desired_branch": "explicit_request_or_block",
                "risk_classes": ["external_non_vontology"],
            },
            {
                "scenario_id": "low_confidence_evidence",
                "desired_branch": "narrow_request_or_defer",
            },
        ],
        "tool_feature_overrides": {
            "workflow_bind_event": {
                "blast_radius": "high",
                "target_criticality": "workflow_process",
                "process_sensitive": True,
                "requires_clear_request": True,
                "organisation_policy_sensitive": True,
            },
            "workflow_create_schedule": {
                "blast_radius": "high",
                "target_criticality": "workflow_process",
                "process_sensitive": True,
                "requires_clear_request": True,
                "organisation_policy_sensitive": True,
            },
            "workflow_set_event_binding_enabled": {
                "blast_radius": "high",
                "target_criticality": "workflow_process",
                "process_sensitive": True,
                "requires_clear_request": True,
                "organisation_policy_sensitive": True,
            },
            "workflow_set_schedule_enabled": {
                "blast_radius": "high",
                "target_criticality": "workflow_process",
                "process_sensitive": True,
                "requires_clear_request": True,
                "organisation_policy_sensitive": True,
            },
            "workflow_delete_event_binding": {
                "blast_radius": "high",
                "target_criticality": "workflow_process",
                "process_sensitive": True,
                "organisation_policy_sensitive": True,
            },
            "workflow_delete_schedule": {
                "blast_radius": "high",
                "target_criticality": "workflow_process",
                "process_sensitive": True,
                "organisation_policy_sensitive": True,
            },
            "merge_concepts": {
                "blast_radius": "high",
                "target_criticality": "knowledge_base",
                "process_sensitive": True,
            },
            "remove_relationships_bulk": {
                "blast_radius": "high",
                "target_criticality": "knowledge_base",
                "process_sensitive": True,
            },
        },
    },
)
_CANONICAL_PROFILE_BY_CONCEPT_ID: dict[str, dict[str, Any]] = {
    str(item["profile_concept_id"]): dict(item)
    for item in _CANONICAL_MINIMAL_IMPOSITION_RUNTIME_PROFILE_BLUEPRINTS
}


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(text)
    return tuple(ordered)


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _profile_display_name(profile: Mapping[str, Any]) -> str:
    profile_id = _safe_str(profile.get("profile_id")) or "minimal imposition runtime"
    pretty = profile_id.replace("_", " ").strip()
    return f"{pretty[:1].upper() + pretty[1:] if pretty else 'Minimal imposition runtime'} profile"


def canonical_minimal_imposition_runtime_profile_concept_ids() -> tuple[str, ...]:
    return tuple(
        str(item["profile_concept_id"])
        for item in _CANONICAL_MINIMAL_IMPOSITION_RUNTIME_PROFILE_BLUEPRINTS
    )


def canonical_minimal_imposition_runtime_profile_blueprints() -> tuple[
    dict[str, Any], ...
]:
    return tuple(
        dict(item) for item in _CANONICAL_MINIMAL_IMPOSITION_RUNTIME_PROFILE_BLUEPRINTS
    )


def _normalise_profile(
    raw_profile: Mapping[str, Any],
    *,
    profile_concept_id: str,
) -> dict[str, Any]:
    decision_policy = raw_profile.get("decision_policy")
    decision_policy = (
        dict(decision_policy) if isinstance(decision_policy, Mapping) else {}
    )
    scenario_policies = raw_profile.get("scenario_policies")
    scenario_policies = (
        list(scenario_policies) if isinstance(scenario_policies, Sequence) else []
    )
    tool_feature_overrides = raw_profile.get("tool_feature_overrides")
    raw_tool_overrides = (
        dict(tool_feature_overrides)
        if isinstance(tool_feature_overrides, Mapping)
        else {}
    )
    normalised_tool_overrides: dict[str, dict[str, Any]] = {}
    for tool_name, override in raw_tool_overrides.items():
        cleaned_tool = _safe_str(tool_name)
        if not cleaned_tool or not isinstance(override, Mapping):
            continue
        normalised_tool_overrides[cleaned_tool.lower()] = dict(override)
    tool_risk_classes = raw_profile.get("tool_risk_classes")
    raw_tool_risk_classes = (
        dict(tool_risk_classes) if isinstance(tool_risk_classes, Mapping) else {}
    )
    normalised_tool_risk_classes: dict[str, str] = {}
    for tool_name, raw_risk_class in raw_tool_risk_classes.items():
        cleaned_tool = _safe_str(tool_name)
        cleaned_risk_class = _safe_str(raw_risk_class)
        if (
            not cleaned_tool
            or not cleaned_risk_class
            or cleaned_risk_class not in _VALID_WRITE_RISK_CLASSES
        ):
            continue
        normalised_tool_risk_classes[cleaned_tool.lower()] = cleaned_risk_class

    return {
        "profile_concept_id": profile_concept_id,
        "profile_id": _safe_str(raw_profile.get("profile_id")) or profile_concept_id,
        "description": _safe_str(raw_profile.get("description")),
        "decision_policy": {
            **dict(_DEFAULT_DECISION_POLICY),
            **decision_policy,
        },
        "scenario_policies": [
            dict(item) for item in scenario_policies if isinstance(item, Mapping)
        ],
        "tool_risk_classes": normalised_tool_risk_classes,
        "tool_feature_overrides": normalised_tool_overrides,
    }


def canonical_minimal_imposition_runtime_tool_risk_classes() -> dict[str, str]:
    merged: dict[str, str] = {}
    for profile in _CANONICAL_MINIMAL_IMPOSITION_RUNTIME_PROFILE_BLUEPRINTS:
        raw_tool_risk_classes = profile.get("tool_risk_classes")
        if not isinstance(raw_tool_risk_classes, Mapping):
            continue
        for tool_name, raw_risk_class in raw_tool_risk_classes.items():
            cleaned_tool = _safe_str(tool_name)
            cleaned_risk_class = _safe_str(raw_risk_class)
            if (
                not cleaned_tool
                or not cleaned_risk_class
                or cleaned_risk_class not in _VALID_WRITE_RISK_CLASSES
            ):
                continue
            merged[cleaned_tool.lower()] = cleaned_risk_class
    return merged


def resolve_runtime_profile_write_tool_risk_class(
    tool_name: str | None,
    *,
    runtime_profile: Mapping[str, Any] | None = None,
) -> str | None:
    cleaned_tool = _safe_str(tool_name)
    if not cleaned_tool:
        return None
    lowered = cleaned_tool.lower()

    if isinstance(runtime_profile, Mapping):
        tool_risk_classes = runtime_profile.get("tool_risk_classes")
        if isinstance(tool_risk_classes, Mapping):
            raw_risk_class = tool_risk_classes.get(lowered) or tool_risk_classes.get(
                cleaned_tool
            )
            cleaned_risk_class = _safe_str(raw_risk_class)
            if cleaned_risk_class in _VALID_WRITE_RISK_CLASSES:
                return cleaned_risk_class

    return canonical_minimal_imposition_runtime_tool_risk_classes().get(lowered)


def resolve_minimal_imposition_runtime_profile_concept_id(
    *,
    workflow_id: str | None = None,
    profile_concept_id: str | None = None,
) -> str | None:
    explicit_id = _safe_str(profile_concept_id)
    if explicit_id:
        return explicit_id

    workflow_concept_id = _safe_str(workflow_id)
    if workflow_concept_id:
        for predicate in _WORKFLOW_LINK_TEXT_PREDICATES:
            texts = get_texts_for_concept(
                workflow_concept_id,
                predicate=predicate,
                limit=1,
            )
            if not texts:
                continue
            text = _safe_str((texts[0] or {}).get("text"))
            if text and text.startswith("#V#"):
                return text

    return DEFAULT_MINIMAL_IMPOSITION_RUNTIME_PROFILE_CONCEPT_ID


def load_minimal_imposition_runtime_profile(
    *,
    workflow_id: str | None = None,
    profile_concept_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    resolved_profile_id = resolve_minimal_imposition_runtime_profile_concept_id(
        workflow_id=workflow_id,
        profile_concept_id=profile_concept_id,
    )
    diagnostics: dict[str, Any] = {
        "requested_workflow_id": _safe_str(workflow_id),
        "requested_profile_concept_id": _safe_str(profile_concept_id),
        "resolved_profile_concept_id": resolved_profile_id,
        "loaded_profile_concept_id": None,
        "source_predicate": None,
        "missing_profile_concept_ids": [],
        "malformed_profile_concept_ids": [],
    }
    if not resolved_profile_id:
        diagnostics["missing_profile_concept_ids"] = [None]
        return None, diagnostics

    concept_doc = _safe_get_concept(resolved_profile_id)
    if not isinstance(concept_doc, Mapping):
        diagnostics["missing_profile_concept_ids"] = [resolved_profile_id]
        return None, diagnostics

    for predicate in _PROFILE_TEXT_PREDICATES:
        texts = get_texts_for_concept(
            resolved_profile_id, predicate=predicate, limit=10
        )
        for row in texts:
            text = _safe_str((row or {}).get("text"))
            if not text:
                continue
            try:
                raw_profile = json.loads(text)
            except json.JSONDecodeError:
                diagnostics["malformed_profile_concept_ids"].append(resolved_profile_id)
                continue
            if isinstance(raw_profile, Mapping):
                diagnostics["loaded_profile_concept_id"] = resolved_profile_id
                diagnostics["source_predicate"] = predicate
                return (
                    _normalise_profile(
                        raw_profile,
                        profile_concept_id=resolved_profile_id,
                    ),
                    diagnostics,
                )

    diagnostics["missing_profile_concept_ids"] = [resolved_profile_id]
    return None, diagnostics


def ensure_canonical_minimal_imposition_runtime_profiles(
    *,
    concept_ids: Sequence[str] | None = None,
    profile_predicate: str = DEFAULT_MINIMAL_IMPOSITION_RUNTIME_PROFILE_PREDICATE,
    workflow_link_predicate: str = MINIMAL_IMPOSITION_RUNTIME_PROFILE_LINK_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    garbage_collect: bool = True,
    create_missing_concepts: bool = True,
    link_workflow_ids: Sequence[str] | None = DEFAULT_PROFILE_WORKFLOW_IDS,
) -> dict[str, Any]:
    requested_concept_ids = (
        _normalise_strings(concept_ids)
        or canonical_minimal_imposition_runtime_profile_concept_ids()
    )
    workflow_ids = _normalise_strings(link_workflow_ids)
    provenance_payload = dict(provenance) if isinstance(provenance, Mapping) else None
    context_payload = dict(context) if isinstance(context, Mapping) else None
    type_created = False
    created_profile_concept_ids: list[str] = []
    persisted_profile_concept_ids: list[str] = []
    linked_workflow_ids: list[str] = []
    unknown_canonical_profile_concept_ids: list[str] = []
    missing_concept_ids: list[str] = []
    errors_by_concept_id: dict[str, str] = {}

    profile_type = _safe_get_concept(MINIMAL_IMPOSITION_RUNTIME_PROFILE_TYPE_ID)
    if not isinstance(profile_type, Mapping) and create_missing_concepts:
        try:
            concept_service.create_concept(
                name="Minimal imposition runtime profile",
                concept_id=MINIMAL_IMPOSITION_RUNTIME_PROFILE_TYPE_ID,
                description=(
                    "Type for canonical minimal-imposition runtime profiles used "
                    "by workflow-governed write-policy and escalation decisions."
                ),
                parent_concept_ids=["#V#thing"],
                create_as_instance=False,
            )
            type_created = True
        except Exception as exc:
            errors_by_concept_id[MINIMAL_IMPOSITION_RUNTIME_PROFILE_TYPE_ID] = (
                f"type_create_failed:{exc}"
            )

    for concept_id in requested_concept_ids:
        seed_profile = _CANONICAL_PROFILE_BY_CONCEPT_ID.get(concept_id)
        if not isinstance(seed_profile, Mapping):
            unknown_canonical_profile_concept_ids.append(concept_id)
            continue

        concept_doc = _safe_get_concept(concept_id)
        if not isinstance(concept_doc, Mapping):
            if not create_missing_concepts:
                missing_concept_ids.append(concept_id)
                continue
            try:
                concept_service.create_concept(
                    name=_profile_display_name(seed_profile),
                    concept_id=concept_id,
                    description=_safe_str(seed_profile.get("description")),
                    parent_concept_ids=[MINIMAL_IMPOSITION_RUNTIME_PROFILE_TYPE_ID],
                    create_as_instance=True,
                )
                created_profile_concept_ids.append(concept_id)
                concept_doc = _safe_get_concept(concept_id)
            except Exception as exc:
                errors_by_concept_id[concept_id] = f"create_failed:{exc}"
                continue

        if not isinstance(concept_doc, Mapping):
            missing_concept_ids.append(concept_id)
            continue

        profile_json = json.dumps(
            dict(seed_profile),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=profile_predicate,
                lang=language,
                text=profile_json,
                policy=policy,
                provenance=provenance_payload,
                context=context_payload,
                garbage_collect=garbage_collect,
            )
            persisted_profile_concept_ids.append(concept_id)
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"profile_upsert_failed:{exc}"
            continue

    for workflow_id in workflow_ids:
        workflow_doc = _safe_get_concept(workflow_id)
        if not isinstance(workflow_doc, Mapping):
            continue
        for concept_id in persisted_profile_concept_ids:
            try:
                upsert_singleton_text_relation(
                    subject_concept_id=workflow_id,
                    predicate=workflow_link_predicate,
                    lang=language,
                    text=concept_id,
                    policy=policy,
                    provenance=provenance_payload,
                    context=context_payload,
                    garbage_collect=garbage_collect,
                )
                linked_workflow_ids.append(workflow_id)
            except Exception as exc:
                errors_by_concept_id[f"{workflow_id}:{concept_id}"] = (
                    f"workflow_link_failed:{exc}"
                )

    return {
        "success": not errors_by_concept_id and not missing_concept_ids,
        "profile_type_created": type_created,
        "created_profile_concept_ids": created_profile_concept_ids,
        "persisted_profile_concept_ids": persisted_profile_concept_ids,
        "linked_workflow_ids": sorted(set(linked_workflow_ids)),
        "unknown_canonical_profile_concept_ids": unknown_canonical_profile_concept_ids,
        "missing_concept_ids": missing_concept_ids,
        "errors_by_concept_id": errors_by_concept_id,
    }


__all__ = [
    "DEFAULT_MINIMAL_IMPOSITION_RUNTIME_PROFILE_CONCEPT_ID",
    "DEFAULT_MINIMAL_IMPOSITION_RUNTIME_PROFILE_PREDICATE",
    "MINIMAL_IMPOSITION_RUNTIME_PROFILE_LINK_PREDICATE",
    "MINIMAL_IMPOSITION_RUNTIME_PROFILE_TYPE_ID",
    "canonical_minimal_imposition_runtime_profile_blueprints",
    "canonical_minimal_imposition_runtime_profile_concept_ids",
    "canonical_minimal_imposition_runtime_tool_risk_classes",
    "ensure_canonical_minimal_imposition_runtime_profiles",
    "load_minimal_imposition_runtime_profile",
    "resolve_runtime_profile_write_tool_risk_class",
    "resolve_minimal_imposition_runtime_profile_concept_id",
]
