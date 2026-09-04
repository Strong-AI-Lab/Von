"""Publish independently useful workflow-support artefacts.

The former universal conversation-turn controller is deliberately absent from
this publication surface. Ordinary ``/von/generate`` turns use the direct
adaptive engine; specialised workflows and reusable explicit-workflow support
remain publishable when their user job independently warrants them. A bounded
retirement tombstone also replaces one previously published, now-unused graph.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    GENERAL_MAIL_REVIEW_WORKFLOW_ID,
    GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from ..workflows.vontology_loader import resolve_workflow_publication_lifecycle
from ..workflows.workflow_concept_authority_service import (
    upsert_workflow_publication_lifecycle,
)
from .synthesiser_context_framing_service import (
    SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
)
from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

_MANAGED_BY = "conversation_turn_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2600"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "canonical_workflow_publication_seed_bundle.json"
)

_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID = "#V#missing_tool_call_retry_prompt"
_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID = (
    "#V#prompt_turn_execution_postcondition_critic"
)
_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID = "#V#tool_call_repair_prompt"
WORKFLOW_STEP_STRUCTURED_OUTPUT_BACKFILL_PROMPT_CONCEPT_ID = (
    "#V#workflow_step_structured_output_backfill_prompt"
)
_RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_ID = (
    "#V#workflow_experience_context_prelude"
)
_RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_LIFECYCLE: Mapping[str, Any] = {
    "phase": "retired",
    "published": False,
    "review_state": "retired",
    "review_reason": (
        "Phase 0 evidence did not justify the recency-based guidance prelude; "
        "the repository seed now carries only an actionless terminal tombstone."
    ),
    "routing_eligible": False,
    "rollout_state": "retired",
    "approval_required": False,
}

_PROMPT_SPECS: tuple[WorkflowPromptConceptSpec, ...] = (
    WorkflowPromptConceptSpec(
        concept_id=_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID,
        name="Missing tool-call retry prompt",
        description=(
            "Repair support for an explicitly invoked tool workflow when its "
            "model response omitted executable tool-call JSON."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
    WorkflowPromptConceptSpec(
        concept_id=_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID,
        name="Effect workflow postcondition critic prompt",
        description=(
            "Postcondition evidence review for concrete consequential workflows "
            "that independently require it; not a universal turn gate."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
    WorkflowPromptConceptSpec(
        concept_id=_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID,
        name="Tool-call repair prompt",
        description=(
            "One-shot schema repair for an explicitly selected tool workflow."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
    WorkflowPromptConceptSpec(
        concept_id=WORKFLOW_STEP_STRUCTURED_OUTPUT_BACKFILL_PROMPT_CONCEPT_ID,
        name="Structured workflow-step output backfill prompt",
        description=(
            "Continue an active structured-output workflow step after tool "
            "evidence has accumulated."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
    WorkflowPromptConceptSpec(
        concept_id=SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
        name="Synthesiser context framing template",
        description=(
            "Represented system-message framing for a selected synthesiser step."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
)

_PROMPT_SEED_PATHS: Mapping[str, Path] = {
    _MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID: (
        _REPO_SEED_ASSET_PATH.parent / "missing_tool_call_retry_prompt_seed.md"
    ),
    _POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID: (
        _REPO_SEED_ASSET_PATH.parent
        / "prompt_turn_execution_postcondition_critic_seed.md"
    ),
    _TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID: (
        _REPO_SEED_ASSET_PATH.parent / "tool_call_repair_prompt_seed.md"
    ),
    WORKFLOW_STEP_STRUCTURED_OUTPUT_BACKFILL_PROMPT_CONCEPT_ID: (
        _REPO_SEED_ASSET_PATH.parent
        / "workflow_step_structured_output_backfill_prompt_seed.md"
    ),
    SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID: (
        _REPO_SEED_ASSET_PATH.parent / "synthesiser_context_framing_prompt_seed.json"
    ),
}

_TARGET_WORKFLOW_IDS: tuple[str, ...] = (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    GENERAL_MAIL_REVIEW_WORKFLOW_ID,
    GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    _RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_ID,
)


def _load_prompt_seed_text(prompt_concept_id: str) -> str:
    path = _PROMPT_SEED_PATHS[prompt_concept_id]
    prompt_text = path.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError(f"workflow_support_prompt_seed_missing:{prompt_concept_id}")
    return prompt_text


def _ensure_conversation_turn_prompt_support(
    *,
    force_prompt_seed: bool = False,
    ensure_tool_evidence_contracts: bool = False,
) -> dict[str, Any]:
    """Ensure prompts retained for explicit workflows and concrete effects."""

    report = ensure_prompt_concept_support(
        prompt_specs=_PROMPT_SPECS,
        provenance_source=_MANAGED_BY,
    )

    support_bootstraps: dict[str, Any] = {}
    if ensure_tool_evidence_contracts:
        from .gmail_tool_evidence_contract_vontology_service import (
            bootstrap_gmail_tool_evidence_contract,
        )
        from .grounded_read_tool_evidence_contract_vontology_service import (
            bootstrap_grounded_read_tool_evidence_contract,
        )
        from .jira_tool_evidence_contract_vontology_service import (
            bootstrap_jira_tool_evidence_contract,
        )

        for name, bootstrap in (
            ("gmail_tool_evidence_contract", bootstrap_gmail_tool_evidence_contract),
            ("jira_tool_evidence_contract", bootstrap_jira_tool_evidence_contract),
            (
                "grounded_read_tool_evidence_contract",
                bootstrap_grounded_read_tool_evidence_contract,
            ),
        ):
            try:
                support_bootstraps[name] = bootstrap()
            except Exception as exc:
                support_bootstraps[name] = {
                    "success": False,
                    "error": str(exc),
                }

    seeded_prompt_ids: list[str] = []
    for prompt_spec in _PROMPT_SPECS:
        prompt_id = prompt_spec.concept_id
        if force_prompt_seed or not prompt_concept_has_content(prompt_id):
            upsert_singleton_text_relation(
                subject_concept_id=prompt_id,
                predicate="hasContent",
                text=_load_prompt_seed_text(prompt_id),
                lang="en-NZ",
                context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
                garbage_collect=True,
            )
            seeded_prompt_ids.append(prompt_id)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
    for prompt_spec in _PROMPT_SPECS:
        prompt_id = prompt_spec.concept_id
        if not prompt_concept_has_content(prompt_id):
            continue
        errors_by_target.pop(prompt_id, None)
        missing_content_prompt_ids = [
            item for item in missing_content_prompt_ids if item != prompt_id
        ]
        if prompt_id not in validated_prompt_ids:
            validated_prompt_ids.append(prompt_id)

    report["validated_prompt_ids"] = validated_prompt_ids
    report["errors_by_target"] = errors_by_target
    report["missing_content_prompt_ids"] = missing_content_prompt_ids
    report["support_bootstraps"] = support_bootstraps
    report["source"] = _SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["counts"] = {
        "created_prompts": len(report.get("created_prompt_ids") or []),
        "validated_prompts": len(validated_prompt_ids),
        "missing_content_prompts": len(missing_content_prompt_ids),
        "linked_workflows": len(report.get("linked_workflow_ids") or []),
        "errors": len(errors_by_target),
    }
    support_bootstrap_success = all(
        bool(value.get("success"))
        for value in support_bootstraps.values()
        if isinstance(value, Mapping)
    )
    report["success"] = (
        not errors_by_target
        and not missing_content_prompt_ids
        and support_bootstrap_success
    )
    return report


def _ensure_retired_workflow_tombstones() -> dict[str, Any]:
    """Make the replaced experience prelude inert and non-discoverable.

    The terminal seed graph closes the exact-ID execution path.  This lifecycle
    write independently closes discovery and routing, with canonical read-back
    so bootstrap cannot report success on an unverified retirement.
    """

    workflow_id = _RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_ID
    expected = _RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_LIFECYCLE
    from .workflow_discovery_service import (
        invalidate_workflow_discovery_executability_caches,
    )

    def _matches(lifecycle: Any) -> bool:
        return isinstance(lifecycle, Mapping) and all(
            lifecycle.get(key) == value for key, value in expected.items()
        )

    try:
        current, current_source = resolve_workflow_publication_lifecycle(workflow_id)
        if _matches(current):
            invalidate_workflow_discovery_executability_caches()
            return {
                "success": True,
                "workflow_id": workflow_id,
                "updated": False,
                "lifecycle": dict(current),
                "lifecycle_source": current_source,
            }

        stored = upsert_workflow_publication_lifecycle(
            workflow_id=workflow_id,
            **expected,
        )
        invalidate_workflow_discovery_executability_caches()
        resolved, resolved_source = resolve_workflow_publication_lifecycle(workflow_id)
        success = _matches(resolved)
        report = {
            "success": success,
            "workflow_id": workflow_id,
            "updated": True,
            "stored_lifecycle": dict(stored),
            "lifecycle": dict(resolved) if isinstance(resolved, Mapping) else None,
            "lifecycle_source": resolved_source,
        }
        if not success:
            report["error"] = "retired_workflow_lifecycle_readback_mismatch"
        return report
    except Exception as exc:  # noqa: BLE001 - bootstrap returns a failure receipt.
        return {
            "success": False,
            "workflow_id": workflow_id,
            "updated": False,
            "error": str(exc),
        }


def bootstrap_canonical_conversation_turn_workflows(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish explicit support workflows and enforce bounded retirements."""

    prompt_support = _ensure_conversation_turn_prompt_support(
        force_prompt_seed=bool(force_republish),
        ensure_tool_evidence_contracts=True,
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=_TARGET_WORKFLOW_IDS,
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    publication_errors = dict(
        (publication.get("publication") or {}).get("errors_by_workflow_id") or {}
    )
    retirement_publication_error = publication_errors.get(
        _RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_ID
    )
    if retirement_publication_error:
        # Do not turn an unknown live graph into a retired workflow by changing
        # only its lifecycle metadata.  The reviewed migration must first
        # replace the known predecessor with the terminal tombstone.
        retirement = {
            "success": False,
            "workflow_id": _RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_ID,
            "updated": False,
            "skipped": True,
            "error": "retired_workflow_tombstone_not_materialised",
            "publication_error": str(retirement_publication_error),
        }
    else:
        retirement = _ensure_retired_workflow_tombstones()
    return {
        "success": bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0
        and bool(retirement.get("success")),
        "workflow_ids": list(_TARGET_WORKFLOW_IDS),
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "retirement": retirement,
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": (
            publication.get("validation_by_workflow_id") or {}
        ),
    }


__all__ = [
    "WORKFLOW_STEP_STRUCTURED_OUTPUT_BACKFILL_PROMPT_CONCEPT_ID",
    "bootstrap_canonical_conversation_turn_workflows",
]
