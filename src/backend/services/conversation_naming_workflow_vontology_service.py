"""Bootstrap represented authority for the owned unnamed-conversation naming consumer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

CONVERSATION_NAMING_WORKFLOW_ID = "#V#unnamed_conversation_naming_workflow"
CONVERSATION_NAMING_PROMPT_CONCEPT_ID = "#V#unnamed_conversation_naming_prompt"

_MANAGED_BY = "conversation_naming_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2698"
_SEED_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "workflows" / "repo_seed_bundles"
)
_REPO_SEED_ASSET_PATH = (
    _SEED_DIRECTORY / "unnamed_conversation_naming_workflow_seed_bundle.json"
)
_PROMPT_SEED_ASSET_PATH = _SEED_DIRECTORY / "unnamed_conversation_naming_prompt_seed.md"


def _load_conversation_naming_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("conversation_naming_prompt_seed_missing")
    return prompt_text


def _ensure_conversation_naming_prompt_support(
    *, force_prompt_seed: bool = False
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=CONVERSATION_NAMING_PROMPT_CONCEPT_ID,
                name="Owned unnamed-conversation naming prompt",
                description=(
                    "Canonical prompt for evidence-grounded conversation naming "
                    "with existing-name preservation and bounded rename evidence."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        CONVERSATION_NAMING_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=CONVERSATION_NAMING_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_conversation_naming_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(CONVERSATION_NAMING_PROMPT_CONCEPT_ID)

    result = dict(report)
    result["seeded_prompt_ids"] = seeded_prompt_ids
    result["seeded_prompt_count"] = len(seeded_prompt_ids)
    result["success"] = bool(
        prompt_concept_has_content(CONVERSATION_NAMING_PROMPT_CONCEPT_ID)
    )
    return result


def bootstrap_canonical_conversation_naming_workflow(
    *, force_republish: bool = False
) -> dict[str, Any]:
    """Seed missing represented naming authority; never create an actor schedule."""

    prompt_support = _ensure_conversation_naming_prompt_support(
        force_prompt_seed=bool(force_republish)
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=(CONVERSATION_NAMING_WORKFLOW_ID,),
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    return {
        "success": bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0,
        "workflow_ids": [CONVERSATION_NAMING_WORKFLOW_ID],
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "support_concepts": publication.get("support_concepts") or {},
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = [
    "CONVERSATION_NAMING_PROMPT_CONCEPT_ID",
    "CONVERSATION_NAMING_WORKFLOW_ID",
    "_ensure_conversation_naming_prompt_support",
    "_load_conversation_naming_prompt_seed_text",
    "bootstrap_canonical_conversation_naming_workflow",
]
