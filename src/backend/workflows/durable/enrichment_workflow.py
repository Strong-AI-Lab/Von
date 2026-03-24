"""Parameterised text-relation enrichment workflow.

A single durable workflow that can generate ANY text relation for concepts
that lack it — descriptions, considerations for use, notes, etc.  This
replaces the need for separate per-predicate workflows by accepting the
target predicate and prompt as runtime parameters.

Usage examples (via MCP ``workflow_create_instance``):
    # Generate missing descriptions
    { "workflow_id": "#V#enrichment_workflow",
      "context": { "predicate": "hasDescription",
                    "prompt_concept_id": "#V#generate_concept_description_prompt",
                    "limit": 20 } }

    # Generate missing considerations
    { "workflow_id": "#V#enrichment_workflow",
      "context": { "predicate": "#V#has_considerations_for_use",
                    "prompt_template": "Write considerations for use for {concept_name} ...",
                    "limit": 50 } }

States:  collect → batch → generate → complete | failed
See JVNAUTOSCI-923.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from ...services.prompt_template_service import PromptTemplateService
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..action_registry import (
    ActionSpec,
    ActionRegistry,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..workflow_registry import WorkflowRegistration
from ...services.workflow_description_vontology_service import (
    DESCRIPTION_PROMPT_CONCEPT_ID,
    build_deterministic_workflow_description,
    build_workflow_description_prompt_context,
    resolve_workflow_description_prompt_concept_id,
)

logger = logging.getLogger(__name__)

# Workflow ID
ENRICHMENT_WORKFLOW_ID = "#V#enrichment_workflow"

DEFAULT_BATCH_SIZE = 5
DEFAULT_CANDIDATE_LIMIT = 50
_WORKFLOW_DESCRIPTION_PREDICATES = {"hasDescription", "#V#hasDescription"}

def build_enrichment_workflow_test_definition() -> WorkflowDefinition:
    """Build the parameterised enrichment workflow definition.

    Context keys consumed:
        - predicate (required): The text relation predicate to check and generate
          (e.g. ``"hasDescription"``, ``"#V#has_considerations_for_use"``)
        - prompt_concept_id (optional): Vontology concept ID holding the prompt
        - prompt_template (optional): Inline prompt template string with {concept_name},
          {concept_id}, {type_hierarchy}, {relationships}, {predicate} placeholders
        - limit (optional, default 50): Max concepts to process
        - batch_size (optional, default 5): Concepts per batch
        - kind_filter (optional): Restrict to concept kind (type/individual/predicate)
        - force_concept_ids (optional): Explicit list of concept IDs to process

    Context keys produced:
        - candidate_ids, total_candidates, batch_index
        - processed_count, failed_count, skipped_count
        - enrichment_result: Final summary dict
    """

    collect = WorkflowStateSpec(
        state_id="collect",
        actions=(
            WorkflowActionInvocation(
                action_id="enrichment.collect_candidates",
                description="Find concepts missing the target text relation.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="batch",
                condition=lambda ctx: bool(ctx.get("candidate_ids")),
                reason="candidates_found",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="no_candidates",
            ),
        ),
    )

    batch = WorkflowStateSpec(
        state_id="batch",
        actions=(
            WorkflowActionInvocation(
                action_id="enrichment.prepare_batch",
                description="Prepare next batch of concepts.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="generate",
                condition=lambda ctx: bool(ctx.get("current_batch")),
                reason="batch_ready",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="all_processed",
            ),
        ),
    )

    generate = WorkflowStateSpec(
        state_id="generate",
        actions=(
            WorkflowActionInvocation(
                action_id="enrichment.process_batch",
                description="Generate and store text relations for batch.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="batch",
                condition=lambda ctx: ctx.get("has_more_batches", False),
                reason="more_batches_pending",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="processing_complete",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="enrichment.finalise",
                description="Report results.",
            ),
        ),
        terminal=True,
    )

    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=ENRICHMENT_WORKFLOW_ID,
        initial_state="collect",
        states={
            "collect": collect,
            "batch": batch,
            "generate": generate,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose="Generate missing text relations for concepts (parameterised by predicate).",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_concepts_missing_predicate(
    predicate: str,
    limit: int,
    kind_filter: Optional[str] = None,
) -> List[str]:
    """Find concept IDs that lack a text relation with the given predicate."""
    from ...db.repositories.text_value_repository import TextRelationsRepository
    from ...db.repositories.concepts_repository import ConceptsRepository

    # Find all concept IDs that already have this predicate
    existing_rels = TextRelationsRepository.find(
        {"predicate": predicate}, projection={"subject_concept_id": 1}
    )
    excluded_ids = {r["subject_concept_id"] for r in existing_rels}

    query: Dict[str, Any] = {}
    if excluded_ids:
        query["concept_id"] = {"$nin": list(excluded_ids)}

    # Optionally filter by kind (requires checking relationships)
    if kind_filter == "individual":
        query["relationships.is_an_instance_of"] = {"$exists": True, "$ne": []}
    elif kind_filter == "type":
        query["relationships.is_a_type_of"] = {"$exists": True, "$ne": []}

    candidates = ConceptsRepository.find(
        query, limit=limit, projection={"concept_id": 1}
    )
    return [c["concept_id"] for c in candidates if c.get("concept_id")]


def _build_concept_context(concept: Dict[str, Any]) -> Dict[str, str]:
    """Build template context from a concept document for prompt formatting."""
    from ...vontology.utils_vontology import (
        get_concept_display_name_with_names_fallback,
        get_concept_description,
    )

    concept_id = concept.get("concept_id", "")
    concept_name = get_concept_display_name_with_names_fallback(concept)
    if not concept_name:
        concept_name = concept_id.replace("#V#", "").replace("_", " ").title()

    relationships = concept.get("relationships", {}) or {}

    # Build type hierarchy
    hierarchy_parts = []
    parents = relationships.get("is_a_type_of", [])
    if parents:
        hierarchy_parts.append(f"is a type of: {', '.join(parents)}")
    instance_of = relationships.get("is_an_instance_of", [])
    if instance_of:
        hierarchy_parts.append(f"is an instance of: {', '.join(instance_of)}")
    type_hierarchy = (
        "; ".join(hierarchy_parts) if hierarchy_parts else "No type hierarchy"
    )

    # Build other relationships (limit to avoid overwhelming prompt)
    rel_parts = []
    skip = {"is_a_type_of", "is_an_instance_of", "has_instance", "has_subtype"}
    for key, values in relationships.items():
        if key in skip:
            continue
        if isinstance(values, list) and values:
            limited = values[:5]
            suffix = f"... (+{len(values) - 5} more)" if len(values) > 5 else ""
            rel_parts.append(f"{key}: {', '.join(str(v) for v in limited)}{suffix}")
    relationships_str = (
        "; ".join(rel_parts[:10]) if rel_parts else "No additional relationships"
    )

    description = get_concept_description(concept) or ""

    context = {
        "concept_name": concept_name,
        "concept_id": concept_id,
        "type_hierarchy": type_hierarchy,
        "relationships": relationships_str,
        "description": description,
    }
    workflow_context = build_workflow_description_prompt_context(concept_id)
    if workflow_context:
        context.update(workflow_context)
    return context


def _resolve_prompt_template(
    ctx: Dict[str, Any],
    *,
    concept_id: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Resolve the authoritative prompt template for enrichment.

    Inline prompt templates remain supported for explicitly parameterised
    enrichment runs. Otherwise prompt bodies must resolve from Vontology prompt
    concepts; there is no silent Python fallback.
    """
    # 1. Explicit inline template
    inline_prompt = ctx.get("prompt_template")
    if isinstance(inline_prompt, str) and inline_prompt.strip():
        return inline_prompt, {
            "source": "inline_template",
            "prompt_concept_id": None,
            "available": True,
            "error": None,
        }

    # 2. Vontology-stored prompt
    prompt_concept_id = ctx.get("prompt_concept_id")
    predicate = str(ctx.get("predicate") or "").strip()
    if not prompt_concept_id and predicate in _WORKFLOW_DESCRIPTION_PREDICATES:
        workflow_context = (
            build_workflow_description_prompt_context(concept_id)
            if isinstance(concept_id, str) and concept_id.strip()
            else {}
        )
        if workflow_context:
            prompt_concept_id = resolve_workflow_description_prompt_concept_id(
                workflow_id=ENRICHMENT_WORKFLOW_ID
            )
        else:
            prompt_concept_id = DESCRIPTION_PROMPT_CONCEPT_ID
    if prompt_concept_id:
        try:
            prompt_service = PromptTemplateService(default_max_chars=12000)
            _resolved_prompt_id, prompt_text = prompt_service.resolve_prompt_text(
                [str(prompt_concept_id).strip()],
                fallback=None,
                max_chars=12000,
            )
            if isinstance(prompt_text, str) and prompt_text.strip():
                return prompt_text, {
                    "source": "vontology_prompt_concept",
                    "prompt_concept_id": str(prompt_concept_id).strip(),
                    "available": True,
                    "error": None,
                }
        except Exception as e:
            logger.debug(
                "Could not load prompt from Vontology %s: %s", prompt_concept_id, e
            )
            return None, {
                "source": "vontology_prompt_concept",
                "prompt_concept_id": str(prompt_concept_id).strip(),
                "available": False,
                "error": f"prompt_lookup_failed:{e}",
            }

    # 3. Fail closed when no authoritative prompt source is available.
    return None, {
        "source": "none",
        "prompt_concept_id": (
            str(prompt_concept_id).strip()
            if isinstance(prompt_concept_id, str) and prompt_concept_id.strip()
            else None
        ),
        "available": False,
        "error": "enrichment_prompt_unavailable",
    }


class _DefaultEmptyTemplateDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _format_prompt_template(
    prompt_template: str,
    template_context: Mapping[str, str],
) -> str:
    """Format enrichment prompts while tolerating schema evolution."""

    return prompt_template.format_map(_DefaultEmptyTemplateDict(template_context))


def _upsert_text_value_internal(concept_id: str, predicate: str, text: str) -> None:
    """Store a generated text relation without user-context auth checks.

    Used by background workers where Flask request context is unavailable.
    """
    from ...db.repositories.text_value_repository import (
        TextRelationsRepository,
        TextValuesRepository,
    )
    from ...db.repositories.concepts_repository import ConceptsRepository

    now = datetime.now(timezone.utc)
    stored_text = text.strip()
    lang = "en-NZ"

    # Fingerprint for deduplication
    norm = stored_text.replace("\r\n", "\n").replace("\r", "\n")
    norm = re.sub(r"[\t\f\v ]+", " ", norm)
    norm = re.sub(r" *\n *", "\n", norm).lower()
    fingerprint = f"{norm}||{lang.lower()}"

    # Find or create TextValue
    existing_tv = TextValuesRepository.find_one(
        {"fingerprint": fingerprint, "lang": lang}
    )
    if existing_tv:
        tv_id = existing_tv["_id"]
    else:
        res = TextValuesRepository.insert_one(
            {
                "text": stored_text,
                "lang": lang,
                "provenance": {
                    "source": "enrichment_workflow",
                    "model": "llm",
                    "jira": "JVNAUTOSCI-923",
                },
                "fingerprint": fingerprint,
                "created_at": now,
                "updated_at": now,
            }
        )
        tv_id = res.inserted_id

    # Create or update relation
    existing_rel = TextRelationsRepository.find_one(
        {"subject_concept_id": concept_id, "predicate": predicate}
    )
    if existing_rel:
        if existing_rel.get("object_text_id") != tv_id:
            TextRelationsRepository.update_one(
                {"_id": existing_rel["_id"]},
                {"$set": {"object_text_id": tv_id, "updated_at": now}},
            )
    else:
        TextRelationsRepository.insert_one(
            {
                "subject_concept_id": concept_id,
                "predicate": predicate,
                "object_text_id": tv_id,
                "context": {},
                "created_at": now,
                "updated_at": now,
            }
        )
        # Mark concept stale for reindexing
        ConceptsRepository.update_one(
            {"concept_id": concept_id},
            {"$set": {"embedding_status": "stale"}},
        )


def _normalise_forced_concept_ids(
    force_concept_ids: Any,
    force_concept_id: Any | None = None,
) -> List[str]:
    """Normalise caller-supplied forced concept identifiers.

    Event bindings commonly map a single ``event.concept_id`` string, while
    direct workflow calls may pass a list. Accept both forms and return a
    stable, deduplicated list.
    """
    collected: List[str] = []

    def _append(value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text:
            collected.append(text)

    if isinstance(force_concept_ids, (list, tuple, set)):
        for item in force_concept_ids:
            _append(item)
    elif force_concept_ids is not None:
        _append(force_concept_ids)

    _append(force_concept_id)

    deduped: List[str] = []
    seen: set[str] = set()
    for concept_id in collected:
        if concept_id in seen:
            continue
        seen.add(concept_id)
        deduped.append(concept_id)
    return deduped


# ---------------------------------------------------------------------------
# Action Handlers
# ---------------------------------------------------------------------------


def _handle_collect_candidates(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Find concepts missing the target predicate."""
    try:
        ctx = request.data
        predicate = ctx.get("predicate")
        if not predicate:
            return WorkflowActionResult(
                status="failed",
                error="predicate_required: specify which text relation to enrich",
            )

        limit = int(ctx.get("limit", DEFAULT_CANDIDATE_LIMIT))
        kind_filter = ctx.get("kind_filter")

        # If caller provided explicit IDs, use those.
        forced_ids = _normalise_forced_concept_ids(
            ctx.get("force_concept_ids"),
            ctx.get("force_concept_id"),
        )
        if forced_ids:
            candidates = forced_ids
        else:
            candidates = _find_concepts_missing_predicate(
                predicate, limit, kind_filter=kind_filter
            )

        logger.info(
            "[enrichment_workflow] Found %d candidates missing '%s'",
            len(candidates),
            predicate,
        )

        return WorkflowActionResult(
            outputs={
                "candidate_ids": candidates,
                "total_candidates": len(candidates),
                "batch_index": 0,
                "processed_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
            }
        )
    except Exception as e:
        logger.exception("[enrichment_workflow] collect_candidates failed")
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_prepare_batch(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Slice the next batch of concept IDs."""
    try:
        ctx = request.data
        candidates = ctx.get("candidate_ids", [])
        batch_index = ctx.get("batch_index", 0)
        batch_size = int(ctx.get("batch_size", DEFAULT_BATCH_SIZE))

        start = batch_index * batch_size
        end = start + batch_size
        current_batch = candidates[start:end]
        has_more = end < len(candidates)

        return WorkflowActionResult(
            outputs={
                "current_batch": current_batch,
                "batch_index": batch_index + 1,
                "has_more_batches": has_more,
            }
        )
    except Exception as e:
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_process_batch(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Generate and store text for each concept in the current batch.

    Uses the synchronous LLMClient.generate() API.
    """
    try:
        from ...languagemodels.llm_interface import get_llm_client
        from ...db.repositories.concepts_repository import ConceptsRepository

        ctx = request.data
        predicate = ctx.get("predicate", "hasDescription")
        batch = ctx.get("current_batch", [])
        prefer_deterministic_workflow_description = bool(
            ctx.get("prefer_deterministic_workflow_description")
        )
        processed = ctx.get("processed_count", 0)
        failed = ctx.get("failed_count", 0)
        skipped = ctx.get("skipped_count", 0)
        deterministic_fallback_count = ctx.get("deterministic_fallback_count", 0)

        if not batch:
            return WorkflowActionResult(
                outputs={
                    "processed_count": processed,
                    "failed_count": failed,
                    "skipped_count": skipped,
                    "deterministic_fallback_count": deterministic_fallback_count,
                }
            )

        llm = get_llm_client()

        newly_processed = 0
        newly_failed = 0
        newly_skipped = 0
        newly_deterministic_fallback = 0
        prompt_resolution_errors: list[dict[str, Any]] = []

        for concept_id in batch:
            try:
                concept = ConceptsRepository.find_one({"concept_id": concept_id})
                if not concept:
                    newly_skipped += 1
                    continue

                # Build context and format prompt
                tmpl_ctx = _build_concept_context(concept)
                tmpl_ctx["predicate"] = predicate
                prompt_template, prompt_diagnostics = _resolve_prompt_template(
                    ctx,
                    concept_id=concept_id,
                )
                if not prompt_template:
                    logger.warning(
                        "[enrichment_workflow] Prompt unavailable for %s (predicate=%s): %s",
                        concept_id,
                        predicate,
                        prompt_diagnostics,
                    )
                    prompt_resolution_errors.append(
                        {
                            "concept_id": concept_id,
                            "predicate": predicate,
                            "diagnostics": prompt_diagnostics,
                        }
                    )
                    newly_failed += 1
                    continue

                prompt = _format_prompt_template(prompt_template, tmpl_ctx)
                text = ""
                used_deterministic_fallback = False
                if (
                    predicate in _WORKFLOW_DESCRIPTION_PREDICATES
                    and prefer_deterministic_workflow_description
                ):
                    text = build_deterministic_workflow_description(
                        concept_id=concept_id,
                        concept_name=tmpl_ctx.get("concept_name"),
                        existing_description=tmpl_ctx.get("description"),
                        workflow_context=tmpl_ctx,
                    ) or ""
                    used_deterministic_fallback = bool(text)
                else:
                    try:
                        response = llm.generate(prompt, llm_params={"max_tokens": 400})
                        text = (
                            response.strip()
                            if isinstance(response, str)
                            else str(response).strip()
                        )
                    except Exception as llm_exc:
                        if predicate in _WORKFLOW_DESCRIPTION_PREDICATES:
                            fallback_text = build_deterministic_workflow_description(
                                concept_id=concept_id,
                                concept_name=tmpl_ctx.get("concept_name"),
                                existing_description=tmpl_ctx.get("description"),
                                workflow_context=tmpl_ctx,
                            )
                            if fallback_text:
                                text = fallback_text
                                used_deterministic_fallback = True
                                logger.warning(
                                    "[enrichment_workflow] Using deterministic workflow-description fallback for %s after LLM failure: %s",
                                    concept_id,
                                    llm_exc,
                                )
                            else:
                                raise
                        else:
                            raise

                if (
                    predicate in _WORKFLOW_DESCRIPTION_PREDICATES
                    and prefer_deterministic_workflow_description
                    and not text
                ):
                    logger.warning(
                        "[enrichment_workflow] Deterministic workflow-description mode produced no text for %s",
                        concept_id,
                    )
                elif used_deterministic_fallback and prefer_deterministic_workflow_description:
                    logger.info(
                        "[enrichment_workflow] Using deterministic workflow-description mode for %s",
                        concept_id,
                    )

                if text and len(text) >= 10:
                    _upsert_text_value_internal(concept_id, predicate, text)
                    newly_processed += 1
                    if used_deterministic_fallback:
                        newly_deterministic_fallback += 1
                    logger.debug(
                        "[enrichment_workflow] Generated '%s' for %s (%d chars)",
                        predicate,
                        concept_id,
                        len(text),
                    )
                else:
                    logger.warning(
                        "[enrichment_workflow] Empty/short generation for %s (predicate=%s)",
                        concept_id,
                        predicate,
                    )
                    newly_failed += 1

            except Exception as e:
                logger.error(
                    "[enrichment_workflow] Error processing %s: %s", concept_id, e
                )
                newly_failed += 1

        return WorkflowActionResult(
            outputs={
                "processed_count": processed + newly_processed,
                "failed_count": failed + newly_failed,
                "skipped_count": skipped + newly_skipped,
                "deterministic_fallback_count": (
                    deterministic_fallback_count + newly_deterministic_fallback
                ),
                "prompt_resolution_errors": prompt_resolution_errors,
            }
        )
    except Exception as e:
        logger.exception("[enrichment_workflow] process_batch failed")
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Produce a summary of the enrichment run."""
    ctx = request.data
    predicate = ctx.get("predicate", "unknown")
    total = ctx.get("total_candidates", 0)
    processed = ctx.get("processed_count", 0)
    failed = ctx.get("failed_count", 0)
    skipped = ctx.get("skipped_count", 0)
    deterministic_fallback_count = ctx.get("deterministic_fallback_count", 0)

    enrichment_result = {
        "success": failed == 0,
        "predicate": predicate,
        "total_candidates": total,
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
        "deterministic_fallback_count": deterministic_fallback_count,
    }

    logger.info(
        "[enrichment_workflow] Complete: predicate=%s candidates=%d processed=%d failed=%d skipped=%d deterministic_fallback=%d",
        predicate,
        total,
        processed,
        failed,
        skipped,
        deterministic_fallback_count,
    )

    return WorkflowActionResult(outputs={"enrichment_result": enrichment_result})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def build_enrichment_workflow_test_registration() -> WorkflowRegistration:
    """Get the workflow registration for the parameterised enrichment workflow."""
    return WorkflowRegistration(
        workflow_id=ENRICHMENT_WORKFLOW_ID,
        definition=build_enrichment_workflow_test_definition(),
        purpose="Generate missing text relations for concepts (parameterised by predicate).",
        source="built_in",
    )


def register_enrichment_actions(registry: ActionRegistry) -> None:
    """Register enrichment action handlers with an ActionRegistry."""
    actions = [
        ActionSpec(
            action_id="enrichment.collect_candidates",
            handler=_handle_collect_candidates,
            description="Find concepts missing the target text relation.",
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="enrichment.prepare_batch",
            handler=_handle_prepare_batch,
            description="Prepare next batch of concepts.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="enrichment.process_batch",
            handler=_handle_process_batch,
            description="Generate and store text relations for batch.",
            side_effects="write",
        ),
        ActionSpec(
            action_id="enrichment.finalise",
            handler=_handle_finalise,
            description="Produce enrichment summary.",
            side_effects="none",
        ),
    ]
    for spec in actions:
        try:
            registry.register(spec)
        except ValueError:
            pass  # Already registered (hot reload)
