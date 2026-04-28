"""Bootstrap support for multilingual concept-enrichment rumination.

The workflow and prompt are Vontology-governed authority surfaces. Repo-side
seed files exist only to initialise missing Vontology materialisation and prompt
content; normal runtime rendering reads the prompt from Vontology and fails
closed when it is unavailable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .prompt_template_service import RenderedPrompt
from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    WorkflowPromptLinkSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
    render_authoritative_prompt,
    resolve_linked_prompt_concept_id,
    safe_str,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID = (
    "#V#multilingual_concept_enrichment_rumination_workflow"
)
MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID = (
    "#V#multilingual_concept_translation_prompt"
)
MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_LINK_PREDICATE = (
    "#V#has_multilingual_concept_translation_prompt"
)

_MANAGED_BY = "multilingual_concept_enrichment_vontology_service"
_SOURCE_TAG = "multilingual_concept_enrichment_rumination"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "multilingual_concept_enrichment_workflow_seed_bundle.json"
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "multilingual_concept_translation_prompt_seed.md"
)
_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_LINK_PREDICATE,
    "has_multilingual_concept_translation_prompt",
    "hasMultilingualConceptTranslationPrompt",
)


def _load_multilingual_concept_translation_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("multilingual_concept_translation_prompt_seed_missing")
    return prompt_text


def resolve_multilingual_concept_translation_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_WORKFLOW_LINK_TEXT_PREDICATES,
        default_prompt_concept_id=MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID,
    )


def render_multilingual_concept_translation_prompt(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 24000,
) -> tuple[RenderedPrompt | None, dict[str, Any]]:
    """Render the authoritative multilingual concept-translation prompt."""

    resolved_prompt_id = resolve_multilingual_concept_translation_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="multilingual_concept_enrichment",
    )
    diagnostics["requested_workflow_id"] = safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = safe_str(prompt_concept_id)
    return rendered, diagnostics


def _ensure_multilingual_concept_translation_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID,
                name="Multilingual concept translation prompt",
                description=(
                    "Canonical prompt for adding faithful Chinese, Spanish, and "
                    "French concept names and descriptions from high-quality "
                    "English source descriptions."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
                prompt_concept_id=MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID,
                predicate=MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_LINK_PREDICATE,
                context={"source": _MANAGED_BY, "seed_role": "missing_prompt_link"},
                reason="multilingual_concept_translation_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_multilingual_concept_translation_prompt_seed_text(),
            lang="en-NZ",
            context={"source": _MANAGED_BY, "source_tag": _SOURCE_TAG},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID)

    report = dict(report)
    report["prompt_concept_id"] = MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = bool(
        prompt_concept_has_content(MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID)
    )
    return report


def bootstrap_canonical_multilingual_concept_enrichment_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish prompt support and the workflow graph seed into Vontology."""

    prompt_support = _ensure_multilingual_concept_translation_prompt_support(
        force_prompt_seed=bool(force_republish),
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=(MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,),
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    return {
        "success": bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0,
        "workflow_ids": [MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID],
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = [
    "MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID",
    "MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_CONCEPT_ID",
    "MULTILINGUAL_CONCEPT_TRANSLATION_PROMPT_LINK_PREDICATE",
    "_ensure_multilingual_concept_translation_prompt_support",
    "_load_multilingual_concept_translation_prompt_seed_text",
    "bootstrap_canonical_multilingual_concept_enrichment_workflow",
    "render_multilingual_concept_translation_prompt",
    "resolve_multilingual_concept_translation_prompt_concept_id",
]
