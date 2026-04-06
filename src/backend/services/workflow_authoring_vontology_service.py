"""Authoritative prompt support for workflow authoring and repair flows.

The workflow-authoring prompts are durable policy artefacts. Repo-side seed
content exists only to bootstrap the authoritative Vontology text relations and
to fail closed with explicit diagnostics when that authority is missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    normalise_strings,
    prompt_concept_has_content,
    safe_str,
)
from ..workflows.workflow_creation_contracts import (
    WORKFLOW_AUTHORING_PROMPT_REPAIR_OR_CREATE_DECISION,
    WORKFLOW_AUTHORING_PROMPT_REPAIR_SPEC,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID,
    WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID,
    WORKFLOW_CREATION_WORKFLOW_ID,
)

WORKFLOW_AUTHORING_PROMPT_SEED_BUNDLE_SCHEMA_VERSION = (
    "workflow_authoring_prompt_seed_bundle.v1"
)
WORKFLOW_AUTHORING_PROMPT_CONTRACT_SCHEMA_VERSION = (
    "workflow_authoring_prompt_contract.v1"
)
WORKFLOW_AUTHORING_PROMPT_CONTENT_PREDICATE = "hasContent"
WORKFLOW_AUTHORING_PROMPT_LANGUAGE = "en-NZ"
WORKFLOW_AUTHORING_PROMPT_BUNDLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "workflow_authoring_prompt_seed_bundle.json"
)
DEFAULT_WORKFLOW_AUTHORING_WORKFLOW_IDS: tuple[str, ...] = (
    WORKFLOW_CREATION_WORKFLOW_ID,
    WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID,
)
WORKFLOW_AUTHORING_PROMPT_CONCEPT_IDS: tuple[str, ...] = (
    WORKFLOW_AUTHORING_PROMPT_REPAIR_OR_CREATE_DECISION,
    WORKFLOW_AUTHORING_PROMPT_REPAIR_SPEC,
)


def _load_prompt_bundle() -> dict[str, Any]:
    payload = json.loads(WORKFLOW_AUTHORING_PROMPT_BUNDLE_PATH.read_text("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("workflow_authoring_prompt_seed_bundle_not_mapping")
    schema_version = safe_str(payload.get("schema_version"))
    if schema_version != WORKFLOW_AUTHORING_PROMPT_SEED_BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            "workflow_authoring_prompt_seed_bundle_schema_unsupported:"
            f"{schema_version or 'missing'}"
        )
    prompts = payload.get("prompts")
    if not isinstance(prompts, Sequence) or isinstance(prompts, (str, bytes, bytearray)):
        raise ValueError("workflow_authoring_prompt_seed_bundle_prompts_missing")
    return {"schema_version": schema_version, "prompts": list(prompts)}


def build_workflow_authoring_prompt_contract() -> dict[str, Any]:
    """Return the machine-readable contract injected into authoring prompt context."""

    return {
        "schema_version": WORKFLOW_AUTHORING_PROMPT_CONTRACT_SCHEMA_VERSION,
        "workflow_ids": list(DEFAULT_WORKFLOW_AUTHORING_WORKFLOW_IDS),
        "prompt_concept_ids": {
            "repair_or_create_decision": WORKFLOW_AUTHORING_PROMPT_REPAIR_OR_CREATE_DECISION,
            "repair_spec": WORKFLOW_AUTHORING_PROMPT_REPAIR_SPEC,
        },
        "context_inputs": {
            "repair_or_create": {
                "request_text_key": "workflow_authoring_request_text",
                "candidate_workflows_key": "workflow_authoring_candidate_workflows",
                "contract_key": "workflow_authoring_prompt_contract",
            },
            "repair_spec": {
                "target_workflow_id_key": "target_workflow_id",
                "existing_workflow_spec_key": "existing_workflow_spec",
                "contract_key": "workflow_authoring_prompt_contract",
            },
        },
        "decision_contract": {
            "allowed_decisions": ["reuse", "repair", "create"],
            "required_output_keys": [
                "decision",
                "target_workflow_id",
                "target_workflow_name",
                "reasoning",
                "evidence",
                "response_text",
            ],
        },
        "repair_contract": {
            "required_output_keys": [
                "target_workflow_id",
                "repair_summary",
                "repaired_workflow_spec",
            ],
            "required_authoring_spec_keys": [
                "workflow_id",
                "description",
                "initial_state_key",
                "steps",
            ],
            "managed_workflow_metadata_keys": [
                "routing_profile",
                "discovery_exemplars",
                "background_launch_policy",
                "launch_input_contract",
                "event_bindings",
                "schedule_specs",
            ],
            "step_safety_requirements": [
                "Every executable step must have a failure path.",
                "LLM steps must declare validation_policy.",
            ],
        },
    }


def get_workflow_authoring_prompt_health_status() -> dict[str, Any]:
    """Return standardised prompt-authority diagnostics for workflow authoring."""

    prompt_status: list[dict[str, Any]] = []
    missing_prompt_ids: list[str] = []
    for prompt_id in WORKFLOW_AUTHORING_PROMPT_CONCEPT_IDS:
        has_content = prompt_concept_has_content(prompt_id)
        row = {
            "concept_id": prompt_id,
            "has_content": bool(has_content),
        }
        if not has_content:
            missing_prompt_ids.append(prompt_id)
        prompt_status.append(row)

    return {
        "available": len(missing_prompt_ids) == 0,
        "schema_version": "workflow_authoring_prompt_health.v1",
        "contract_schema_version": WORKFLOW_AUTHORING_PROMPT_CONTRACT_SCHEMA_VERSION,
        "prompt_concepts": prompt_status,
        "missing_prompt_ids": missing_prompt_ids,
    }


def ensure_workflow_authoring_prompt_support(
    *,
    workflow_ids: Sequence[str] | None = DEFAULT_WORKFLOW_AUTHORING_WORKFLOW_IDS,
    language: str = WORKFLOW_AUTHORING_PROMPT_LANGUAGE,
    policy: str = "replace_others",
    garbage_collect: bool = True,
) -> dict[str, Any]:
    """Bootstrap authoritative workflow-authoring prompt concepts and content."""

    bundle = _load_prompt_bundle()
    prompt_rows = [
        dict(item)
        for item in bundle.get("prompts") or []
        if isinstance(item, Mapping)
    ]
    requested_workflow_ids = normalise_strings(workflow_ids)
    prompt_specs = []
    for row in prompt_rows:
        concept_id = safe_str(row.get("concept_id"))
        if not concept_id:
            continue
        type_ids = normalise_strings(row.get("type_ids")) or ("#V#prompt_for_llm",)
        prompt_specs.append(
            WorkflowPromptConceptSpec(
                concept_id=concept_id,
                name=safe_str(row.get("name")) or concept_id,
                description=(
                    safe_str(row.get("description"))
                    or "Canonical workflow-authoring prompt."
                ),
                parent_concept_ids=tuple(type_ids),
                require_content=False,
            )
        )

    report = ensure_prompt_concept_support(
        prompt_specs=tuple(prompt_specs),
        workflow_links=(),
        provenance_source="workflow_authoring_vontology_service",
        language=language,
        policy=policy,
        garbage_collect=garbage_collect,
    )

    seeded_prompt_ids: list[str] = []
    errors_by_target = dict(report.get("errors_by_target") or {})
    for row in prompt_rows:
        concept_id = safe_str(row.get("concept_id"))
        content = safe_str(row.get("content"))
        if not concept_id or not content:
            if concept_id and not content:
                errors_by_target[concept_id] = "prompt_content_missing_from_seed_bundle"
            continue
        try:
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=WORKFLOW_AUTHORING_PROMPT_CONTENT_PREDICATE,
                text=content,
                lang=language,
                policy=policy,
                garbage_collect=garbage_collect,
                provenance={
                    "source": "workflow_authoring_vontology_service",
                    "reason": "workflow_authoring_prompt_content_bootstrap",
                },
                context={
                    "workflow_ids": list(requested_workflow_ids),
                    "seed_bundle": str(WORKFLOW_AUTHORING_PROMPT_BUNDLE_PATH),
                },
            )
            seeded_prompt_ids.append(concept_id)
        except Exception as exc:
            errors_by_target[concept_id] = f"prompt_content_seed_failed:{exc}"

    validated_prompt_ids = [
        prompt_id
        for prompt_id in WORKFLOW_AUTHORING_PROMPT_CONCEPT_IDS
        if prompt_concept_has_content(prompt_id)
    ]
    missing_content_prompt_ids = [
        prompt_id
        for prompt_id in WORKFLOW_AUTHORING_PROMPT_CONCEPT_IDS
        if prompt_id not in validated_prompt_ids
    ]

    report.update(
        {
            "success": not errors_by_target and not missing_content_prompt_ids,
            "workflow_ids": list(requested_workflow_ids),
            "seeded_prompt_ids": list(dict.fromkeys(seeded_prompt_ids)),
            "validated_prompt_ids": validated_prompt_ids,
            "missing_content_prompt_ids": missing_content_prompt_ids,
            "errors_by_target": errors_by_target,
            "prompt_contract": build_workflow_authoring_prompt_contract(),
            "health": get_workflow_authoring_prompt_health_status(),
            "counts": {
                **dict(report.get("counts") or {}),
                "seeded_prompts": len(set(seeded_prompt_ids)),
                "validated_prompts": len(validated_prompt_ids),
                "missing_content_prompts": len(missing_content_prompt_ids),
                "errors": len(errors_by_target),
            },
        }
    )
    return report


__all__ = [
    "DEFAULT_WORKFLOW_AUTHORING_WORKFLOW_IDS",
    "WORKFLOW_AUTHORING_PROMPT_CONCEPT_IDS",
    "WORKFLOW_AUTHORING_PROMPT_CONTRACT_SCHEMA_VERSION",
    "build_workflow_authoring_prompt_contract",
    "ensure_workflow_authoring_prompt_support",
    "get_workflow_authoring_prompt_health_status",
]
