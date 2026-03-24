"""Vontology-backed prompt support for workflow-discovery gap recovery.

The recovery and test workflows fail closed if their authoritative prompts are
missing. Startup validates and links those prompt concepts here so workflow-
governed recovery behaviour can evolve without embedding silent inline
fallbacks in code.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .prompt_template_service import RenderedPrompt
from .workflow_prompt_authority_service import (
    WorkflowPromptConceptSpec,
    WorkflowPromptLinkSpec,
    ensure_prompt_concept_support,
    normalise_strings,
    render_authoritative_prompt,
    resolve_linked_prompt_concept_id,
    safe_str,
)
from ..workflows.workflow_gap_workflow_contracts import (
    DEFAULT_WORKFLOW_GAP_PROMPT_WORKFLOW_IDS,
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
    WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE,
    WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID,
    WORKFLOW_GAP_ANALYSIS_PROMPT_TYPE_ID,
    WORKFLOW_GAP_CANDIDATE_PROMPT_LINK_PREDICATE,
    WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID,
    WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
)

_WORKFLOW_GAP_ANALYSIS_LINK_PREDICATES: tuple[str, ...] = (
    WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE,
    "has_workflow_gap_analysis_prompt",
    "hasWorkflowGapAnalysisPrompt",
)

_WORKFLOW_GAP_TEST_LINK_PREDICATES: tuple[str, ...] = (
    WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE,
    "has_workflow_gap_test_prompt",
    "hasWorkflowGapTestPrompt",
)

_WORKFLOW_GAP_CANDIDATE_LINK_PREDICATES: tuple[str, ...] = (
    WORKFLOW_GAP_CANDIDATE_PROMPT_LINK_PREDICATE,
    "has_workflow_gap_candidate_prompt",
    "hasWorkflowGapCandidatePrompt",
)

def resolve_workflow_gap_analysis_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_WORKFLOW_GAP_ANALYSIS_LINK_PREDICATES,
        default_prompt_concept_id=WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
    )


def resolve_workflow_gap_test_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_WORKFLOW_GAP_TEST_LINK_PREDICATES,
        default_prompt_concept_id=WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID,
    )


def resolve_workflow_gap_candidate_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_WORKFLOW_GAP_CANDIDATE_LINK_PREDICATES,
        default_prompt_concept_id=WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID,
    )


def render_workflow_gap_analysis_prompt(
    *,
    workflow_id: str | None = WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 24000,
) -> tuple[RenderedPrompt | None, dict[str, Any]]:
    resolved_prompt_id = resolve_workflow_gap_analysis_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="workflow_gap_analysis",
    )
    diagnostics["requested_workflow_id"] = safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = safe_str(prompt_concept_id)
    return rendered, diagnostics


def render_workflow_gap_test_prompt(
    *,
    workflow_id: str | None = WORKFLOW_GAP_TEST_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 24000,
) -> tuple[RenderedPrompt | None, dict[str, Any]]:
    resolved_prompt_id = resolve_workflow_gap_test_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="workflow_gap_test",
    )
    diagnostics["requested_workflow_id"] = safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = safe_str(prompt_concept_id)
    return rendered, diagnostics


def render_workflow_gap_candidate_prompt(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 24000,
) -> tuple[RenderedPrompt | None, dict[str, Any]]:
    resolved_prompt_id = resolve_workflow_gap_candidate_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="workflow_gap_candidate",
    )
    diagnostics["requested_workflow_id"] = safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = safe_str(prompt_concept_id)
    return rendered, diagnostics


def ensure_workflow_gap_prompt_support(
    *,
    workflow_ids: Sequence[str] | None = DEFAULT_WORKFLOW_GAP_PROMPT_WORKFLOW_IDS,
    analysis_prompt_concept_id: str = WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
    test_prompt_concept_id: str = WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID,
    prompt_predicate: str = "hasContent",
    language: str = "en-NZ",
    policy: str = "replace_others",
    garbage_collect: bool = True,
) -> dict[str, Any]:
    requested_workflow_ids = set(normalise_strings(workflow_ids))
    workflow_links: list[WorkflowPromptLinkSpec] = []
    if WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID in requested_workflow_ids:
        workflow_links.append(
            WorkflowPromptLinkSpec(
                workflow_id=WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
                prompt_concept_id=analysis_prompt_concept_id,
                predicate=WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE,
                context={"jira": "JVNAUTOSCI-1389"},
                reason="workflow_gap_analysis_prompt_link_bootstrap",
            )
        )
    if WORKFLOW_GAP_TEST_WORKFLOW_ID in requested_workflow_ids:
        workflow_links.append(
            WorkflowPromptLinkSpec(
                workflow_id=WORKFLOW_GAP_TEST_WORKFLOW_ID,
                prompt_concept_id=test_prompt_concept_id,
                predicate=WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE,
                context={"jira": "JVNAUTOSCI-1389"},
                reason="workflow_gap_test_prompt_link_bootstrap",
            )
        )

    prompt_specs = []
    for concept_id, name, description in (
        (
            analysis_prompt_concept_id,
            "Workflow gap analysis prompt",
            "Canonical LLM prompt for workflow-gap recovery analysis.",
        ),
        (
            test_prompt_concept_id,
            "Workflow gap test prompt",
            "Canonical LLM prompt for workflow-gap candidate evaluation.",
        ),
        (
            WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID,
            "Workflow gap candidate execution prompt",
            "Canonical LLM prompt for executing workflow-gap candidate workflows.",
        ),
    ):
        prompt_specs.append(
            WorkflowPromptConceptSpec(
                concept_id=concept_id,
                name=name,
                description=description,
                parent_concept_ids=(WORKFLOW_GAP_ANALYSIS_PROMPT_TYPE_ID,),
            )
        )

    report = ensure_prompt_concept_support(
        prompt_specs=tuple(prompt_specs),
        workflow_links=tuple(workflow_links),
        provenance_source="workflow_gap_vontology_service",
        language=language,
        policy=policy,
        garbage_collect=garbage_collect,
    )
    report["analysis_prompt_concept_id"] = analysis_prompt_concept_id
    report["test_prompt_concept_id"] = test_prompt_concept_id
    report["candidate_prompt_concept_id"] = WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID
    return report


__all__ = [
    "WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID",
    "WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE",
    "WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID",
    "WORKFLOW_GAP_CANDIDATE_PROMPT_LINK_PREDICATE",
    "WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID",
    "WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE",
    "ensure_workflow_gap_prompt_support",
    "render_workflow_gap_candidate_prompt",
    "render_workflow_gap_analysis_prompt",
    "render_workflow_gap_test_prompt",
    "resolve_workflow_gap_candidate_prompt_concept_id",
    "resolve_workflow_gap_analysis_prompt_concept_id",
    "resolve_workflow_gap_test_prompt_concept_id",
]
