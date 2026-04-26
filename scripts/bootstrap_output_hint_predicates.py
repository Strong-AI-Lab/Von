"""Bootstrap the Vontology-authored output-hint predicate family.

JVNAUTOSCI-2117 (Phase 0 of Epic JVNAUTOSCI-2112) introduces six predicates that
bind a tool concept (subject) to a structured hint body (object). This script
ensures each predicate concept exists as an instance of ``#V#predicate`` (per
AGENTS.md section 6.7) and registers a ``hasDescription`` text relation
documenting the predicate's purpose, expected hint-body shape, and downstream
consumer surface.

Run idempotently against either the production database or the test database;
existing concepts and text relations are detected and left untouched (apart
from documentation refresh, which is a safe upsert).

Usage::

    pdm run python scripts/bootstrap_output_hint_predicates.py

The script imports nothing tool-specific and contains no references to any
particular external integration. This is the anti-drift gate for the predicate
family: predicates are generic, and hint bodies (authored separately by
JVNAUTOSCI-2118 and later vertical-slice tasks) carry the tool-specific
knowledge.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_sys_path() -> None:
    repo_root = Path(__file__).parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    src_root = repo_root / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


_bootstrap_sys_path()

from src.backend.services import concept_service  # noqa: E402
from src.backend.services.output_hint_contracts import (  # noqa: E402
    OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
    OUTPUT_FOLLOWUP_HINT_PREDICATE_ID,
    OUTPUT_ITEM_CLASSIFICATION_HINT_PREDICATE_ID,
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
    OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
    OUTPUT_UI_RENDERING_HINT_PREDICATE_ID,
)
from src.backend.services.text_value_service import upsert_text_for_concept  # noqa: E402


PREDICATE_TYPE_CONCEPT_ID = "#V#predicate"


PREDICATE_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    (
        OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
        "output_item_summary_hint",
        (
            "Binds a tool concept (subject) to a hint body (object) that "
            "instructs how an individual item in the tool's output payload "
            "should be summarised. The hint body is a free-form text relation "
            "consumed by the synthesiser-context-prep workflow step, which "
            "injects it into the synthesiser stage so the response-forming "
            "LLM call shapes per-item summaries consistently. The hint body "
            "should describe which fields matter, what to omit, and the "
            "preferred summary style; it must not include tool-implementation "
            "details (those live in the tool's own catalogue entry)."
        ),
    ),
    (
        OUTPUT_ITEM_CLASSIFICATION_HINT_PREDICATE_ID,
        "output_item_classification_hint",
        (
            "Binds a tool concept to a hint body that instructs how items in "
            "the tool's output should be classified (for example into "
            "categories, intents, or priority bands). The hint body should "
            "name the classification axes and their permitted values. Reserved "
            "for future classification consumers; signal extraction proper is "
            "covered by output_item_signal_extraction_hint."
        ),
    ),
    (
        OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
        "output_item_signal_extraction_hint",
        (
            "Binds a tool concept to a hint body that instructs how to "
            "extract structured signals from an item in the tool's output. "
            "Consumed by the generic Python primitive "
            "tool_result_hints.extract_signals_from_tool_result, which "
            "resolves this predicate against the tool concept, assembles an "
            "LLM prompt purely from the resolved hint body plus the tool "
            "payload, and returns the structured signals. The hint body "
            "should describe the desired output schema (field names and "
            "semantics) and any extraction rules; it must not contain "
            "tool-routing or transport concerns."
        ),
    ),
    (
        OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
        "output_collection_presentation_hint",
        (
            "Binds a tool concept to a hint body describing how a collection "
            "of items returned by the tool should be presented as a whole "
            "(ordering, grouping, headline framing, what context the user "
            "needs before the per-item summaries). Should reference the "
            "per-item summary hint by predicate rather than duplicating its "
            "body; resolution happens at synthesis time. Consumed by the "
            "synthesiser-context-prep workflow step."
        ),
    ),
    (
        OUTPUT_UI_RENDERING_HINT_PREDICATE_ID,
        "output_ui_rendering_hint",
        (
            "Binds a tool concept to a hint body describing how the tool's "
            "output should be rendered in the user-facing UI (preferred "
            "component kind, column layout, expandable detail rules, action "
            "affordances). Reserved for the rendering pipeline; not consumed "
            "by the LLM stages."
        ),
    ),
    (
        OUTPUT_FOLLOWUP_HINT_PREDICATE_ID,
        "output_followup_hint",
        (
            "Binds a tool concept to a hint body proposing follow-up actions "
            "or questions the agent should consider after the tool runs "
            "successfully (for example, related tools to invoke, common next "
            "user questions, refinement loops). Consumed by the synthesiser "
            "and follow-up suggestion surfaces."
        ),
    ),
)


def _get_concept(concept_id: str):
    try:
        return concept_service.get_concept_by_concept_id(concept_id)
    except Exception:
        return None


def _ensure_predicate_concept(
    concept_id: str,
    name: str,
    description: str,
) -> bool:
    """Create the predicate concept if missing. Returns True if created."""

    if _get_concept(concept_id):
        print(f"[bootstrap] exists: {concept_id}")
        return False
    print(f"[bootstrap] creating predicate concept: {concept_id}")
    concept_service.create_concept(
        concept_id=concept_id,
        name=name,
        description=description,
        instance_of_type=PREDICATE_TYPE_CONCEPT_ID,
    )
    return True


def _upsert_predicate_documentation(concept_id: str, description: str) -> None:
    """Upsert a hasDescription text relation documenting the predicate."""

    upsert_text_for_concept(
        subject_concept_id=concept_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        context={"source": "bootstrap_output_hint_predicates"},
    )


def bootstrap_output_hint_predicates() -> dict[str, int]:
    """Idempotently bootstrap all six output-hint predicates.

    Returns counts of created vs existing predicates for caller diagnostics.
    """

    created = 0
    existing = 0
    for concept_id, name, description in PREDICATE_DEFINITIONS:
        if _ensure_predicate_concept(concept_id, name, description):
            created += 1
        else:
            existing += 1
        _upsert_predicate_documentation(concept_id, description)
    return {"created": created, "existing": existing}


def main() -> int:
    summary = bootstrap_output_hint_predicates()
    print(
        f"[bootstrap] done. created={summary['created']} "
        f"existing={summary['existing']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
