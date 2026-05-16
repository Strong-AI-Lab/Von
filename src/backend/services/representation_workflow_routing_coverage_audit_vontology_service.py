"""Vontology-backed support for representation-routing coverage audits.

The audit profile is represented policy. Python loads and validates it, and
startup may seed a default profile when the authoritative Vontology concept is
missing. Repair judgement and workflow improvement suggestions are made by the
audit workflow's represented LLM prompt, not by this support service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .representation_workflow_routing_coverage_audit_contracts import (
    CANONICAL_REPRESENTATION_ROUTING_AUDIT_WORKFLOW_IDS,
    REPRESENTATION_ROUTING_AUDIT_PROFILE_SCHEMA_VERSION,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_LINK_PREDICATE,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_PREDICATE,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_TYPE_ID,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_LINK_PREDICATE,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
)
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    WorkflowPromptLinkSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

_MANAGED_BY = "representation_workflow_routing_coverage_audit_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2331"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "representation_workflow_routing_coverage_audit_workflow_seed_bundle.json"
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_representation_workflow_routing_coverage_audit_seed.md"
)
_PROFILE_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "representation_workflow_routing_coverage_audit_profile_seed.json"
)

_PROFILE_TEXT_PREDICATES: tuple[str, ...] = (
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_PREDICATE,
    "hasRepresentationWorkflowRoutingCoverageAuditProfileJson",
    "has_representation_workflow_routing_coverage_audit_profile_json",
    "hasContent",
)
_WORKFLOW_PROFILE_LINK_PREDICATES: tuple[str, ...] = (
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_LINK_PREDICATE,
    "hasRepresentationWorkflowRoutingCoverageAuditProfile",
    "has_representation_workflow_routing_coverage_audit_profile",
)

_REQUIRED_AUDIT_POLICY_FIELDS: tuple[str, ...] = (
    "max_cases",
    "max_discovery_results",
    "discovery_relevance_threshold",
    "discovery_timeout_seconds",
    "minimum_expected_presence_rate",
    "minimum_expected_top_rank_rate",
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_strings(value: Any, *, limit: int = 100) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _safe_str(item)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _coerce_positive_int(
    value: Any, *, default: int, minimum: int, maximum: int
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_probability(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _coerce_timeout(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.1, min(60.0, parsed))


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _ensure_concept(
    *,
    concept_id: str,
    name: str,
    description: str,
    parent_concept_ids: Sequence[str],
    create_as_instance: bool,
    created_ids: list[str],
    errors: dict[str, str],
) -> None:
    if isinstance(_safe_get_concept(concept_id), Mapping):
        return
    try:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            description=description,
            parent_concept_ids=list(parent_concept_ids),
            create_as_instance=create_as_instance,
            visibility_scope_mode="global_general",
        )
        created_ids.append(concept_id)
    except Exception as exc:
        errors[concept_id] = f"concept_create_failed:{exc}"


def ensure_representation_workflow_routing_coverage_audit_support_concepts() -> (
    dict[str, Any]
):
    """Ensure generic Vontology concepts needed to store audit authority."""

    created_ids: list[str] = []
    errors: dict[str, str] = {}
    _ensure_concept(
        concept_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
        name="Representation Workflow Routing Coverage Audit Workflow",
        description=(
            "Workflow that audits routing coverage for representation workflows "
            "and delegates repair judgement to a represented prompt."
        ),
        parent_concept_ids=("#V#ai_workflow", "#V#durable_workflow"),
        create_as_instance=True,
        created_ids=created_ids,
        errors=errors,
    )
    _ensure_concept(
        concept_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_TYPE_ID,
        name="Representation workflow routing coverage audit profile",
        description=(
            "Type for represented policies that define workflow routing coverage "
            "audit probes, inventory scope, and evidence budgets."
        ),
        parent_concept_ids=("#V#thing",),
        create_as_instance=False,
        created_ids=created_ids,
        errors=errors,
    )
    _ensure_concept(
        concept_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID,
        name="Representation workflow routing coverage default audit profile",
        description=(
            "Default represented profile for auditing coverage of general "
            "representation workflow routing classes."
        ),
        parent_concept_ids=(
            REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_TYPE_ID,
        ),
        create_as_instance=True,
        created_ids=created_ids,
        errors=errors,
    )
    for concept_id, name, description in (
        (
            REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_PREDICATE,
            "Has representation workflow routing coverage audit profile JSON",
            "Links an audit profile concept to its structured JSON policy body.",
        ),
        (
            REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_LINK_PREDICATE,
            "Has representation workflow routing coverage audit profile",
            "Links an audit workflow to the represented profile it uses.",
        ),
        (
            REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_LINK_PREDICATE,
            "Has representation workflow routing coverage audit prompt",
            "Links the audit workflow to the represented prompt that judges evidence.",
        ),
    ):
        _ensure_concept(
            concept_id=concept_id,
            name=name,
            description=description,
            parent_concept_ids=("#V#predicate", "#V#binary_predicate"),
            create_as_instance=True,
            created_ids=created_ids,
            errors=errors,
        )
    return {
        "success": not errors,
        "created_concept_ids": created_ids,
        "errors_by_target": errors,
    }


def _load_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("representation_routing_audit_prompt_seed_missing")
    return prompt_text


def _load_profile_seed_payload() -> dict[str, Any]:
    with _PROFILE_SEED_ASSET_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("representation_routing_audit_profile_seed_not_object")
    profile, errors = _normalise_profile(
        payload,
        profile_concept_id=(
            REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID
        ),
    )
    if profile is None or errors:
        raise ValueError(
            "representation_routing_audit_profile_seed_invalid:"
            + ",".join(errors or ["unknown"])
        )
    return payload


def _normalise_audit_policy(raw_policy: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(raw_policy, Mapping):
        return None, ["audit_policy_missing"]
    policy = raw_policy
    errors = [
        f"audit_policy_missing_{field}"
        for field in _REQUIRED_AUDIT_POLICY_FIELDS
        if field not in policy
    ]
    if errors:
        return None, errors
    return {
        "max_cases": _coerce_positive_int(
            policy.get("max_cases"),
            default=16,
            minimum=1,
            maximum=200,
        ),
        "max_discovery_results": _coerce_positive_int(
            policy.get("max_discovery_results"),
            default=6,
            minimum=1,
            maximum=50,
        ),
        "discovery_relevance_threshold": _coerce_probability(
            policy.get("discovery_relevance_threshold"),
            default=0.05,
        ),
        "discovery_timeout_seconds": _coerce_timeout(
            policy.get("discovery_timeout_seconds"),
            default=8.0,
        ),
        "minimum_expected_presence_rate": _coerce_probability(
            policy.get("minimum_expected_presence_rate"),
            default=0.9,
        ),
        "minimum_expected_top_rank_rate": _coerce_probability(
            policy.get("minimum_expected_top_rank_rate"),
            default=0.75,
        ),
    }, []


def _normalise_inventory_policy(
    raw_policy: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(raw_policy, Mapping):
        return None, ["inventory_policy_missing"]
    policy = raw_policy
    mode = _safe_str(policy.get("mode"))
    if not mode:
        return None, ["inventory_policy_missing_mode"]
    if mode not in {"expected_workflows", "explicit_workflows", "all_vontology"}:
        return None, [f"inventory_policy_invalid_mode:{mode}"]
    if "max_workflows" not in policy:
        return None, ["inventory_policy_missing_max_workflows"]
    return {
        "mode": mode,
        "max_workflows": _coerce_positive_int(
            policy.get("max_workflows"),
            default=80,
            minimum=1,
            maximum=500,
        ),
        "workflow_ids": _normalise_strings(policy.get("workflow_ids"), limit=500),
    }, []


def _normalise_audit_cases(raw_cases: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        return [], ["audit_cases_missing"]

    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            errors.append(f"audit_case_invalid:{index}")
            continue
        case_id = _safe_str(raw_case.get("case_id"))
        query = _safe_str(raw_case.get("query") or raw_case.get("request_text"))
        expected_workflow_ids = _normalise_strings(
            raw_case.get("expected_workflow_ids") or raw_case.get("target_workflow_ids")
        )
        if not case_id:
            errors.append(f"audit_case_missing_case_id:{index}")
            continue
        if case_id.lower() in seen_ids:
            errors.append(f"audit_case_duplicate_case_id:{case_id}")
            continue
        if not query:
            errors.append(f"audit_case_missing_query:{case_id}")
            continue
        if not expected_workflow_ids:
            errors.append(f"audit_case_missing_expected_workflow_ids:{case_id}")
            continue
        seen_ids.add(case_id.lower())
        cases.append(
            {
                "case_id": case_id,
                "task_class": _safe_str(raw_case.get("task_class")) or "representation",
                "query": query,
                "expected_workflow_ids": expected_workflow_ids,
                "acceptable_workflow_ids": _normalise_strings(
                    raw_case.get("acceptable_workflow_ids")
                ),
                "priority": _safe_str(raw_case.get("priority")) or "medium",
                "notes": _safe_str(raw_case.get("notes")),
            }
        )
    if not cases:
        errors.append("audit_cases_empty")
    return cases, errors


def _normalise_profile(
    raw_profile: Mapping[str, Any],
    *,
    profile_concept_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    profile = dict(raw_profile)
    if _safe_str(profile.get("schema_version")) not in {
        REPRESENTATION_ROUTING_AUDIT_PROFILE_SCHEMA_VERSION,
        "representation_routing_coverage_audit_profile.v1",
    }:
        return None, ["unsupported_schema_version"]
    audit_cases, case_errors = _normalise_audit_cases(profile.get("audit_cases"))
    if case_errors:
        return None, case_errors
    audit_policy, audit_policy_errors = _normalise_audit_policy(
        profile.get("audit_policy")
    )
    if audit_policy_errors:
        return None, audit_policy_errors
    inventory_policy, inventory_policy_errors = _normalise_inventory_policy(
        profile.get("inventory_policy")
    )
    if inventory_policy_errors:
        return None, inventory_policy_errors
    return (
        {
            "schema_version": REPRESENTATION_ROUTING_AUDIT_PROFILE_SCHEMA_VERSION,
            "profile_concept_id": profile_concept_id,
            "profile_id": _safe_str(profile.get("profile_id")) or profile_concept_id,
            "description": _safe_str(profile.get("description")),
            "audit_policy": audit_policy or {},
            "inventory_policy": inventory_policy or {},
            "evaluation_dimensions": (
                list(profile.get("evaluation_dimensions"))
                if isinstance(profile.get("evaluation_dimensions"), list)
                else []
            ),
            "suggestion_policy": (
                dict(profile.get("suggestion_policy"))
                if isinstance(profile.get("suggestion_policy"), Mapping)
                else {}
            ),
            "self_improvement_contract": (
                dict(profile.get("self_improvement_contract"))
                if isinstance(profile.get("self_improvement_contract"), Mapping)
                else {}
            ),
            "audit_cases": audit_cases,
        },
        [],
    )


def resolve_representation_workflow_routing_coverage_audit_profile_concept_id(
    *,
    workflow_id: str | None = None,
    profile_concept_id: str | None = None,
) -> str | None:
    explicit_id = _safe_str(profile_concept_id)
    if explicit_id:
        return explicit_id

    workflow_concept_id = _safe_str(workflow_id)
    if workflow_concept_id:
        for predicate in _WORKFLOW_PROFILE_LINK_PREDICATES:
            rows = get_texts_for_concept(
                workflow_concept_id,
                predicate=predicate,
                limit=1,
            )
            if not rows:
                continue
            text = _safe_str((rows[0] or {}).get("text"))
            if text and text.startswith("#V#"):
                return text

    return REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID


def load_representation_workflow_routing_coverage_audit_profile(
    *,
    workflow_id: str | None = None,
    profile_concept_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    resolved_profile_id = (
        resolve_representation_workflow_routing_coverage_audit_profile_concept_id(
            workflow_id=workflow_id,
            profile_concept_id=profile_concept_id,
        )
    )
    diagnostics: dict[str, Any] = {
        "requested_workflow_id": _safe_str(workflow_id),
        "requested_profile_concept_id": _safe_str(profile_concept_id),
        "resolved_profile_concept_id": resolved_profile_id,
        "loaded_profile_concept_id": None,
        "source_predicate": None,
        "validation_errors": [],
        "error_code": None,
    }
    if not resolved_profile_id:
        diagnostics["error_code"] = "representation_routing_audit_profile_unavailable"
        return None, diagnostics

    if not isinstance(_safe_get_concept(resolved_profile_id), Mapping):
        diagnostics["error_code"] = "representation_routing_audit_profile_unavailable"
        return None, diagnostics

    for predicate in _PROFILE_TEXT_PREDICATES:
        rows = get_texts_for_concept(
            resolved_profile_id,
            predicate=predicate,
            limit=10,
        )
        for row in rows:
            text = _safe_str((row or {}).get("text"))
            if not text:
                continue
            try:
                raw_profile = json.loads(text)
            except json.JSONDecodeError:
                diagnostics["validation_errors"].append(
                    f"profile_json_invalid:{predicate}"
                )
                continue
            if not isinstance(raw_profile, Mapping):
                diagnostics["validation_errors"].append(
                    f"profile_payload_not_mapping:{predicate}"
                )
                continue
            profile, errors = _normalise_profile(
                raw_profile,
                profile_concept_id=resolved_profile_id,
            )
            diagnostics["loaded_profile_concept_id"] = resolved_profile_id
            diagnostics["source_predicate"] = predicate
            if errors:
                diagnostics["validation_errors"] = errors
                diagnostics["error_code"] = (
                    "representation_routing_audit_profile_invalid"
                )
                return None, diagnostics
            return profile, diagnostics

    diagnostics["error_code"] = "representation_routing_audit_profile_unavailable"
    return None, diagnostics


def ensure_representation_workflow_routing_coverage_audit_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    support = ensure_representation_workflow_routing_coverage_audit_support_concepts()
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=(
                    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
                ),
                name="Representation workflow routing coverage audit prompt",
                description=(
                    "Canonical prompt for judging representation workflow routing "
                    "coverage evidence and authoring workflow-targeted improvement "
                    "suggestions only when the evidence warrants them."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
                prompt_concept_id=(
                    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
                ),
                predicate=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_LINK_PREDICATE,
                context={"jira": _SOURCE_TAG},
                reason="representation_routing_audit_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=(
                REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
            ),
            predicate="hasContent",
            text=_load_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(
            REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
        )

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = _normalise_strings(
        report.get("missing_content_prompt_ids"),
        limit=100,
    )
    validated_prompt_ids = _normalise_strings(
        report.get("validated_prompt_ids"),
        limit=100,
    )
    for prompt_id in seeded_prompt_ids:
        if prompt_concept_has_content(prompt_id):
            if errors_by_target.get(prompt_id) == "prompt_content_missing":
                errors_by_target.pop(prompt_id, None)
            missing_content_prompt_ids = [
                item for item in missing_content_prompt_ids if item != prompt_id
            ]
            if prompt_id not in validated_prompt_ids:
                validated_prompt_ids.append(prompt_id)

    if not bool(support.get("success")):
        errors_by_target.update(dict(support.get("errors_by_target") or {}))
    report["errors_by_target"] = errors_by_target
    report["missing_content_prompt_ids"] = missing_content_prompt_ids
    report["validated_prompt_ids"] = validated_prompt_ids
    counts = dict(report.get("counts") or {})
    counts["validated_prompts"] = len(validated_prompt_ids)
    counts["missing_content_prompts"] = len(missing_content_prompt_ids)
    counts["errors"] = len(errors_by_target)
    report["counts"] = counts
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["support_concepts"] = support
    report["success"] = bool(
        prompt_concept_has_content(
            REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
        )
    ) and not bool(report.get("errors_by_target"))
    return report


def ensure_canonical_representation_workflow_routing_coverage_audit_profile(
    *,
    force_profile_seed: bool = False,
    language: str = "en-NZ",
) -> dict[str, Any]:
    support = ensure_representation_workflow_routing_coverage_audit_support_concepts()
    created_ids = list(support.get("created_concept_ids") or [])
    errors: dict[str, str] = {}
    errors.update(dict(support.get("errors_by_target") or {}))

    persisted_profile = False
    if (
        force_profile_seed
        or not load_representation_workflow_routing_coverage_audit_profile(
            profile_concept_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID,
        )[0]
    ):
        try:
            seed_payload = _load_profile_seed_payload()
            upsert_singleton_text_relation(
                subject_concept_id=(
                    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID
                ),
                predicate=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_PREDICATE,
                text=json.dumps(seed_payload, ensure_ascii=True, sort_keys=True),
                lang=language,
                context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
                garbage_collect=True,
            )
            persisted_profile = True
        except Exception as exc:
            errors[
                REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID
            ] = f"profile_persist_failed:{exc}"

    linked_workflows: list[str] = []
    for workflow_id in CANONICAL_REPRESENTATION_ROUTING_AUDIT_WORKFLOW_IDS:
        try:
            upsert_singleton_text_relation(
                subject_concept_id=workflow_id,
                predicate=(
                    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_LINK_PREDICATE
                ),
                text=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID,
                lang=language,
                context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
                garbage_collect=True,
            )
            linked_workflows.append(workflow_id)
        except Exception as exc:
            errors[workflow_id] = f"profile_link_failed:{exc}"

    loaded_profile, diagnostics = (
        load_representation_workflow_routing_coverage_audit_profile(
            workflow_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
        )
    )
    return {
        "success": not errors and loaded_profile is not None,
        "profile_type_id": REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_TYPE_ID,
        "profile_concept_id": REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID,
        "created_concept_ids": created_ids,
        "support_concepts": support,
        "persisted_profile": persisted_profile,
        "linked_workflow_ids": linked_workflows,
        "profile_diagnostics": diagnostics,
        "errors_by_target": errors,
    }


def bootstrap_canonical_representation_workflow_routing_coverage_audit_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish prompt/profile support and the audit workflow seed graph."""

    prompt_support = (
        ensure_representation_workflow_routing_coverage_audit_prompt_support(
            force_prompt_seed=bool(force_republish)
        )
    )
    profile_support = (
        ensure_canonical_representation_workflow_routing_coverage_audit_profile(
            force_profile_seed=bool(force_republish)
        )
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=CANONICAL_REPRESENTATION_ROUTING_AUDIT_WORKFLOW_IDS,
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    return {
        "success": bool(prompt_support.get("success"))
        and bool(profile_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0,
        "source": _SOURCE_TAG,
        "managed_by": _MANAGED_BY,
        "workflow_ids": list(CANONICAL_REPRESENTATION_ROUTING_AUDIT_WORKFLOW_IDS),
        "prompt_support": prompt_support,
        "profile_support": profile_support,
        "publication": publication.get("publication"),
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = [
    "bootstrap_canonical_representation_workflow_routing_coverage_audit_workflow",
    "ensure_canonical_representation_workflow_routing_coverage_audit_profile",
    "ensure_representation_workflow_routing_coverage_audit_prompt_support",
    "ensure_representation_workflow_routing_coverage_audit_support_concepts",
    "load_representation_workflow_routing_coverage_audit_profile",
    "resolve_representation_workflow_routing_coverage_audit_profile_concept_id",
]
