"""Bootstrap and prompt support for canonical entity-representation workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .representation_contract_vontology_service import (
    ensure_canonical_representation_contract_profiles,
)
from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle
from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
)
from ..workflows.durable.workflow_creation_workflow import (
    WORKFLOW_CREATION_WORKFLOW_ID,
)
from ..workflows.workflow_template_profile_service import (
    WORKFLOW_CREATION_COMPANY_TEMPLATE_ID,
    WORKFLOW_CREATION_EVENT_TEMPLATE_ID,
    WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
    WORKFLOW_CREATION_PLACE_TEMPLATE_ID,
    resolve_workflow_spec_template,
)

ENTITY_REPRESENTATION_WORKFLOW_ID = "#V#entity_representation_workflow"
PERSON_REPRESENTATION_WORKFLOW_ID = "#V#person_representation_workflow"
COMPANY_REPRESENTATION_WORKFLOW_ID = "#V#company_representation_workflow"
EVENT_REPRESENTATION_WORKFLOW_ID = "#V#event_representation_workflow"
PLACE_REPRESENTATION_WORKFLOW_ID = "#V#place_representation_workflow"
ENTITY_REPRESENTATION_PREFLIGHT_PROMPT_CONCEPT_ID = (
    "#V#entity_representation_preflight_prompt"
)
ENTITY_REPRESENTATION_PAYLOAD_PROMPT_CONCEPT_ID = (
    "#V#entity_representation_payload_prompt"
)

_MANAGED_BY = "entity_representation_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1704"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "entity_representation_workflow_seed_bundle.json"
)
_PREFLIGHT_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "entity_representation_preflight_prompt_seed.md"
)
_PAYLOAD_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "entity_representation_payload_prompt_seed.md"
)

_CANONICAL_ENTITY_EXECUTION_WORKFLOW_SPECS: tuple[dict[str, str], ...] = (
    {
        "domain": "person",
        "workflow_id": PERSON_REPRESENTATION_WORKFLOW_ID,
        "workflow_name": "Person Representation Workflow",
        "workflow_description": (
            "Canonical person representation workflow for conversational text "
            "grounded in Vontology."
        ),
        "request_summary": "represent person from text",
        "template_id": WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
        "request_text": (
            "Create a workflow from this description request: represent a "
            "person from text description in Vontology."
        ),
    },
    {
        "domain": "company",
        "workflow_id": COMPANY_REPRESENTATION_WORKFLOW_ID,
        "workflow_name": "Company Representation Workflow",
        "workflow_description": (
            "Canonical company representation workflow for conversational text "
            "grounded in Vontology."
        ),
        "request_summary": "represent company from text",
        "template_id": WORKFLOW_CREATION_COMPANY_TEMPLATE_ID,
        "request_text": (
            "Create a workflow from this description request: represent a "
            "company from text description in Vontology."
        ),
    },
    {
        "domain": "event",
        "workflow_id": EVENT_REPRESENTATION_WORKFLOW_ID,
        "workflow_name": "Event Representation Workflow",
        "workflow_description": (
            "Canonical event representation workflow for conversational text "
            "grounded in Vontology."
        ),
        "request_summary": "represent event from text",
        "template_id": WORKFLOW_CREATION_EVENT_TEMPLATE_ID,
        "request_text": (
            "Create a workflow from this description request: represent an "
            "event from text description in Vontology."
        ),
    },
    {
        "domain": "place",
        "workflow_id": PLACE_REPRESENTATION_WORKFLOW_ID,
        "workflow_name": "Place Representation Workflow",
        "workflow_description": (
            "Canonical place representation workflow for conversational text "
            "grounded in Vontology."
        ),
        "request_summary": "represent place from text",
        "template_id": WORKFLOW_CREATION_PLACE_TEMPLATE_ID,
        "request_text": (
            "Create a workflow from this description request: represent a "
            "place from text description in Vontology."
        ),
    },
)


def _load_prompt_seed_text(*, asset_path: Path, error_code: str) -> str:
    """Return non-authoritative bootstrap prompt text for missing content."""

    prompt_text = asset_path.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError(error_code)
    return prompt_text


def _ensure_entity_representation_prompt_support() -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=ENTITY_REPRESENTATION_PREFLIGHT_PROMPT_CONCEPT_ID,
                name="Entity representation preflight prompt",
                description=(
                    "Canonical prompt for routing conversational entity "
                    "representation requests across reusable specialised "
                    "person/company/event/place workflows."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=ENTITY_REPRESENTATION_PAYLOAD_PROMPT_CONCEPT_ID,
                name="Entity representation payload prompt",
                description=(
                    "Canonical prompt for extracting minimal structured entity "
                    "payloads from conversational representation requests."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    prompt_seed_specs = (
        (
            ENTITY_REPRESENTATION_PREFLIGHT_PROMPT_CONCEPT_ID,
            _PREFLIGHT_PROMPT_SEED_ASSET_PATH,
            "entity_representation_preflight_prompt_seed_missing",
        ),
        (
            ENTITY_REPRESENTATION_PAYLOAD_PROMPT_CONCEPT_ID,
            _PAYLOAD_PROMPT_SEED_ASSET_PATH,
            "entity_representation_payload_prompt_seed_missing",
        ),
    )
    for prompt_concept_id, asset_path, error_code in prompt_seed_specs:
        if prompt_concept_has_content(prompt_concept_id):
            continue
        upsert_singleton_text_relation(
            subject_concept_id=prompt_concept_id,
            predicate="hasContent",
            text=_load_prompt_seed_text(
                asset_path=asset_path,
                error_code=error_code,
            ),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(prompt_concept_id)

    report = dict(report)
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = bool(
        prompt_concept_has_content(ENTITY_REPRESENTATION_PREFLIGHT_PROMPT_CONCEPT_ID)
        and prompt_concept_has_content(ENTITY_REPRESENTATION_PAYLOAD_PROMPT_CONCEPT_ID)
    )
    return report


def _bootstrap_canonical_entity_execution_workflows() -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    workflow_ids: list[str] = []
    errors: list[str] = []

    for workflow_spec in _CANONICAL_ENTITY_EXECUTION_WORKFLOW_SPECS:
        workflow_id = str(workflow_spec["workflow_id"])
        workflow_ids.append(workflow_id)
        rendered_spec, diagnostics = resolve_workflow_spec_template(
            request_text=str(workflow_spec["request_text"]),
            explicit_template_id=str(workflow_spec["template_id"]),
            variables={
                "workflow_id": workflow_id,
                "workflow_name": str(workflow_spec["workflow_name"]),
                "workflow_description": str(workflow_spec["workflow_description"]),
                "request_summary": str(workflow_spec["request_summary"]),
            },
        )
        definition = build_workflow_definition_from_authoring_spec(rendered_spec)
        publication_report = authority_service.publish_workflow_definition_from_definition(
            definition=definition,
            create_missing=True,
            purpose=str(workflow_spec["workflow_description"]),
        )
        errors_by_workflow_id = publication_report.get("errors_by_workflow_id") or {}
        validation_failures = (
            publication_report.get("validation_failures_by_workflow_id") or {}
        )
        workflow_error = (
            str(errors_by_workflow_id.get(workflow_id) or "").strip()
            if isinstance(errors_by_workflow_id, dict)
            else ""
        )
        validation_failure = (
            validation_failures.get(workflow_id)
            if isinstance(validation_failures, dict)
            else None
        )
        relation_specs = tuple(rendered_spec.get("text_relations") or ())
        if relation_specs:
            authority_service.upsert_seed_bundle_text_relations(
                subject_concept_id=workflow_id,
                relation_specs=relation_specs,
                workflow_id=workflow_id,
                source_tag=_SOURCE_TAG,
                managed_by=_MANAGED_BY,
            )

        report = {
            "domain": workflow_spec["domain"],
            "workflow_id": workflow_id,
            "template_id": workflow_spec["template_id"],
            "template_resolution": diagnostics,
            "publication": publication_report,
            "success": not workflow_error and not validation_failure,
        }
        if workflow_error:
            report["error"] = workflow_error
            errors.append(f"{workflow_id}:{workflow_error}")
        if validation_failure:
            report["validation_failure"] = validation_failure
            errors.append(f"{workflow_id}:validation_failed")
        reports.append(report)

    return {
        "success": not errors,
        "workflow_ids": workflow_ids,
        "reports": reports,
        "errors": errors,
    }


def _ensure_workflow_creation_dependency() -> dict[str, Any]:
    publication = authority_service.publish_canonical_chat_workflow_graphs(
        target_workflow_ids=[WORKFLOW_CREATION_WORKFLOW_ID]
    )
    counts = publication.get("counts") or {}
    return {
        "success": not bool(counts.get("errors")),
        "workflow_ids": [WORKFLOW_CREATION_WORKFLOW_ID],
        "publication": publication,
    }


def bootstrap_canonical_entity_representation_workflows() -> dict[str, Any]:
    """Publish prompt support and the canonical conversational entity workflow."""

    profile_support = ensure_canonical_representation_contract_profiles()
    prompt_support = _ensure_entity_representation_prompt_support()
    workflow_creation_support = _ensure_workflow_creation_dependency()
    domain_workflow_support = _bootstrap_canonical_entity_execution_workflows()
    publication = bootstrap_repo_seed_workflow_bundle(asset_path=_REPO_SEED_ASSET_PATH)
    return {
        "success": bool(prompt_support.get("success"))
        and bool(workflow_creation_support.get("success"))
        and bool(domain_workflow_support.get("success"))
        and not bool(publication.get("publication", {}).get("counts", {}).get("errors")),
        "workflow_ids": [
            WORKFLOW_CREATION_WORKFLOW_ID,
            PERSON_REPRESENTATION_WORKFLOW_ID,
            COMPANY_REPRESENTATION_WORKFLOW_ID,
            EVENT_REPRESENTATION_WORKFLOW_ID,
            PLACE_REPRESENTATION_WORKFLOW_ID,
            ENTITY_REPRESENTATION_WORKFLOW_ID,
        ],
        "profile_support": profile_support,
        "prompt_support": prompt_support,
        "workflow_creation_support": workflow_creation_support,
        "domain_workflow_support": domain_workflow_support,
        "publication": publication.get("publication"),
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = [
    "ENTITY_REPRESENTATION_PAYLOAD_PROMPT_CONCEPT_ID",
    "ENTITY_REPRESENTATION_PREFLIGHT_PROMPT_CONCEPT_ID",
    "ENTITY_REPRESENTATION_WORKFLOW_ID",
    "PERSON_REPRESENTATION_WORKFLOW_ID",
    "COMPANY_REPRESENTATION_WORKFLOW_ID",
    "EVENT_REPRESENTATION_WORKFLOW_ID",
    "PLACE_REPRESENTATION_WORKFLOW_ID",
    "bootstrap_canonical_entity_representation_workflows",
]
