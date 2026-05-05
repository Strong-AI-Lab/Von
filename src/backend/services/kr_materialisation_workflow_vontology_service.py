"""Seed-only publication support for the KR materialisation workflow family.

The KR materialisation repo bundle is a reviewable bootstrap fixture, not the
request-time workflow authority. Runtime execution relies on Vontology workflow
and prompt concepts; this service only seeds or repairs missing materialisation
through the shared repo-seed bootstrap pathway.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import concept_service
from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle
from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_publication_lifecycle,
)
from ..workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)
from ..workflows.workflow_repo_seed_export_service import (
    diff_repo_seed_workflow_bundle_from_authority,
    write_repo_seed_workflow_bundle_from_authority,
)

KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID = (
    "#V#kr_design_concept_materialisation_item_workflow"
)
KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID = (
    "#V#kr_design_relationship_assertion_item_workflow"
)
KR_DESIGN_MATERIALISATION_WORKFLOW_ID = "#V#kr_design_materialisation_workflow"
KR_MATERIALISATION_WORKFLOW_IDS = (
    KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
    KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID,
    KR_DESIGN_MATERIALISATION_WORKFLOW_ID,
)

PROMPT_KR_DESIGN_MATERIALISATION_PLAN_ID = (
    "#V#prompt_kr_design_materialisation_plan"
)
PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID = (
    "#V#prompt_kr_relationship_endpoint_resolution"
)

_MANAGED_BY = "kr_materialisation_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2271"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "kr_materialisation_workflow_seed_bundle.json"
)
_PROMPT_SEED_ASSET_PATHS = {
    PROMPT_KR_DESIGN_MATERIALISATION_PLAN_ID: (
        Path(__file__).resolve().parents[1]
        / "workflows"
        / "repo_seed_bundles"
        / "prompt_kr_design_materialisation_plan_seed.md"
    ),
    PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID: (
        Path(__file__).resolve().parents[1]
        / "workflows"
        / "repo_seed_bundles"
        / "prompt_kr_relationship_endpoint_resolution_seed.md"
    ),
}


def _coerce_relationship_values(raw_value: Any) -> tuple[str, ...]:
    if isinstance(raw_value, str):
        return (raw_value,)
    if isinstance(raw_value, (list, tuple, set)):
        return tuple(
            str(item).strip()
            for item in raw_value
            if isinstance(item, str) and str(item).strip()
        )
    return ()


def _persisted_step_llm_policy_has_prompt_text(step_concept_id: str) -> bool:
    concept_doc = concept_service.get_concept_by_concept_id(step_concept_id)
    relationships = (
        concept_doc.get("relationships") if isinstance(concept_doc, Mapping) else {}
    )
    if not isinstance(relationships, Mapping):
        return False
    input_map_values: list[str] = []
    for predicate in ("hasInputMap", "#V#hasInputMap"):
        input_map_values.extend(_coerce_relationship_values(relationships.get(predicate)))
    for input_map_value in input_map_values:
        if not input_map_value.startswith("workflow_step_llm_policy="):
            continue
        raw_policy = input_map_value.split("=", 1)[1].strip()
        try:
            parsed_policy = json.loads(raw_policy)
        except Exception:
            continue
        if not isinstance(parsed_policy, Mapping):
            continue
        if any(
            key in parsed_policy
            for key in ("prompt_text", "response_contract_text")
        ):
            return True
    return False


def _load_prompt_seed_text(asset_path: Path, *, error_code: str) -> str:
    prompt_text = asset_path.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError(error_code)
    return prompt_text


def _ensure_kr_materialisation_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=PROMPT_KR_DESIGN_MATERIALISATION_PLAN_ID,
                name="KR design materialisation plan prompt",
                description=(
                    "Canonical prompt for extracting bounded knowledge "
                    "representation designs into verified Vontology concept "
                    "and relationship materialisation plans."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID,
                name="KR relationship endpoint resolution prompt",
                description=(
                    "Canonical prompt for resolving KR relationship endpoint "
                    "references against verified concept materialisation "
                    "read-back evidence before relationship assertion."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    for prompt_id, asset_path in _PROMPT_SEED_ASSET_PATHS.items():
        if force_prompt_seed or not prompt_concept_has_content(prompt_id):
            upsert_singleton_text_relation(
                subject_concept_id=prompt_id,
                predicate="hasContent",
                text=_load_prompt_seed_text(
                    asset_path,
                    error_code=f"kr_materialisation_prompt_seed_missing:{prompt_id}",
                ),
                lang="en-NZ",
                context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
                garbage_collect=True,
            )
            seeded_prompt_ids.append(prompt_id)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
    for prompt_id in _PROMPT_SEED_ASSET_PATHS:
        if not prompt_concept_has_content(prompt_id):
            continue
        errors_by_target.pop(prompt_id, None)
        missing_content_prompt_ids = [
            missing_prompt_id
            for missing_prompt_id in missing_content_prompt_ids
            if missing_prompt_id != prompt_id
        ]
        if prompt_id not in validated_prompt_ids:
            validated_prompt_ids.append(prompt_id)

    report["validated_prompt_ids"] = validated_prompt_ids
    report["errors_by_target"] = errors_by_target
    report["missing_content_prompt_ids"] = missing_content_prompt_ids
    report["counts"] = {
        "created_prompts": len(report.get("created_prompt_ids") or []),
        "validated_prompts": len(validated_prompt_ids),
        "missing_content_prompts": len(missing_content_prompt_ids),
        "linked_workflows": len(report.get("linked_workflow_ids") or []),
        "errors": len(errors_by_target),
    }
    report["source"] = _SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = not errors_by_target and not missing_content_prompt_ids
    return report


def _normalise_target_workflow_ids(
    target_workflow_ids: Sequence[str] | None,
) -> tuple[str, ...]:
    if target_workflow_ids is None:
        return KR_MATERIALISATION_WORKFLOW_IDS
    requested = tuple(
        str(workflow_id).strip()
        for workflow_id in target_workflow_ids
        if isinstance(workflow_id, str) and str(workflow_id).strip()
    )
    if not requested:
        return KR_MATERIALISATION_WORKFLOW_IDS
    known = set(KR_MATERIALISATION_WORKFLOW_IDS)
    unknown = sorted(workflow_id for workflow_id in requested if workflow_id not in known)
    if unknown:
        raise ValueError("kr_materialisation_unknown_workflow_id:" + ",".join(unknown))
    return requested


def validate_kr_materialisation_seed_bundle(
    *,
    target_workflow_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate the repo seed bundle's generated definitions without publishing."""

    requested_ids = _normalise_target_workflow_ids(target_workflow_ids)
    bundle = authority_service.load_repo_seed_workflow_bundle(_REPO_SEED_ASSET_PATH)
    publication_specs = {
        workflow_id: spec
        for workflow_id, spec in dict(bundle.get("publication_specs") or {}).items()
        if workflow_id in set(requested_ids)
    }
    publication_definitions = authority_service._build_definition_map_from_publication_specs(
        publication_specs
    )
    supported_action_ids = tuple(bundle.get("supported_action_ids") or ())

    def _definition_loader(workflow_id: str) -> Any | None:
        return publication_definitions.get(str(workflow_id or "").strip())

    validation_by_workflow_id: dict[str, dict[str, Any]] = {}
    for workflow_id in requested_ids:
        definition = publication_definitions.get(workflow_id)
        if definition is None:
            validation_by_workflow_id[workflow_id] = {
                "valid": False,
                "errors": ["publication_definition_missing"],
            }
            continue
        validation_by_workflow_id[workflow_id] = validate_workflow_definition_contract(
            definition=definition,
            supported_action_ids=supported_action_ids,
            known_workflow_ids=requested_ids,
            workflow_definition_loader=_definition_loader,
        )

    invalid_workflow_ids = [
        workflow_id
        for workflow_id, validation in validation_by_workflow_id.items()
        if not bool(validation.get("valid"))
    ]
    return {
        "success": not invalid_workflow_ids,
        "asset_path": str(_REPO_SEED_ASSET_PATH),
        "workflow_ids": list(requested_ids),
        "invalid_workflow_ids": invalid_workflow_ids,
        "validation_by_workflow_id": validation_by_workflow_id,
    }


def bootstrap_canonical_kr_materialisation_workflows(
    *,
    force_republish: bool = False,
    target_workflow_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Seed or repair the canonical KR materialisation workflow family."""

    requested_ids = _normalise_target_workflow_ids(target_workflow_ids)
    prompt_support = _ensure_kr_materialisation_prompt_support(
        force_prompt_seed=bool(force_republish)
    )
    report = dict(
        bootstrap_repo_seed_workflow_bundle(
            asset_path=_REPO_SEED_ASSET_PATH,
            force_republish=force_republish,
            target_workflow_ids=requested_ids,
        )
    )
    publication_counts = dict((report.get("publication") or {}).get("counts") or {})
    report["workflow_ids"] = list(requested_ids)
    report["prompt_support"] = prompt_support
    report["success"] = bool(prompt_support.get("success")) and int(
        publication_counts.get("errors") or 0
    ) == 0
    return report


def verify_canonical_kr_materialisation_workflows(
    *,
    target_workflow_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    requested_ids = _normalise_target_workflow_ids(target_workflow_ids)
    rows: list[dict[str, Any]] = []
    for workflow_id in requested_ids:
        definition = load_workflow_definition_from_vontology(workflow_id)
        lifecycle, lifecycle_source = resolve_workflow_publication_lifecycle(workflow_id)
        states = getattr(definition, "states", {}) if definition is not None else {}
        llm_prompt_contracts: list[dict[str, Any]] = []
        persisted_inline_prompt_text_state_ids: list[str] = []
        if isinstance(states, Mapping):
            for state_id, state in states.items():
                for action in tuple(getattr(state, "actions", ()) or ()):
                    action_id = str(getattr(action, "action_id", "") or "").strip()
                    execution_mode = str(
                        getattr(action, "execution_mode", "") or ""
                    ).strip()
                    if action_id != "llm.action" and execution_mode != "llm":
                        continue
                    prompt_contract = getattr(action, "prompt_contract", None)
                    prompt_contract_map = (
                        dict(prompt_contract)
                        if isinstance(prompt_contract, Mapping)
                        else {}
                    )
                    llm_policy = getattr(action, "llm_policy", None)
                    llm_policy_map = (
                        dict(llm_policy) if isinstance(llm_policy, Mapping) else {}
                    )
                    persisted_has_prompt_text = (
                        _persisted_step_llm_policy_has_prompt_text(str(state_id))
                    )
                    if persisted_has_prompt_text:
                        persisted_inline_prompt_text_state_ids.append(str(state_id))
                    llm_prompt_contracts.append(
                        {
                            "state_id": str(state_id),
                            "requested_prompt_concept_ids": list(
                                prompt_contract_map.get(
                                    "requested_prompt_concept_ids"
                                )
                                or []
                            ),
                            "resolved_prompt_concept_id": prompt_contract_map.get(
                                "resolved_prompt_concept_id"
                            ),
                            "runtime_policy_has_rendered_prompt_text": bool(
                                "prompt_text" in llm_policy_map
                            ),
                            "persisted_policy_has_inline_prompt_text": bool(
                                persisted_has_prompt_text
                            ),
                        }
                    )
        rows.append(
            {
                "workflow_id": workflow_id,
                "loaded": definition is not None,
                "state_count": len(states) if isinstance(states, Mapping) else 0,
                "llm_prompt_contracts": llm_prompt_contracts,
                "persisted_inline_prompt_text_state_ids": (
                    persisted_inline_prompt_text_state_ids
                ),
                "phase": (
                    lifecycle.get("phase") if isinstance(lifecycle, Mapping) else None
                ),
                "published": (
                    lifecycle.get("published")
                    if isinstance(lifecycle, Mapping)
                    else False
                ),
                "publication_lifecycle_source": lifecycle_source,
            }
        )
    return {
        "success": all(
            row["loaded"]
            and row["published"]
            and not row["persisted_inline_prompt_text_state_ids"]
            for row in rows
        ),
        "workflow_ids": list(requested_ids),
        "workflows": rows,
    }


def export_kr_materialisation_workflow_repo_seed_bundle(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Refresh the KR workflow seed bundle from authoritative Vontology state."""

    return write_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path or _REPO_SEED_ASSET_PATH
    )


def diff_kr_materialisation_workflow_repo_seed_bundle(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Diff the KR workflow seed bundle against authoritative Vontology state."""

    return diff_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path or _REPO_SEED_ASSET_PATH
    )


__all__ = [
    "KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID",
    "KR_DESIGN_MATERIALISATION_WORKFLOW_ID",
    "KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID",
    "KR_MATERIALISATION_WORKFLOW_IDS",
    "PROMPT_KR_DESIGN_MATERIALISATION_PLAN_ID",
    "PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID",
    "bootstrap_canonical_kr_materialisation_workflows",
    "diff_kr_materialisation_workflow_repo_seed_bundle",
    "export_kr_materialisation_workflow_repo_seed_bundle",
    "validate_kr_materialisation_seed_bundle",
    "verify_canonical_kr_materialisation_workflows",
]