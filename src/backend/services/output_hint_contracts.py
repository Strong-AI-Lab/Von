"""Canonical concept IDs for the Vontology-authored tool-result hint family.

JVNAUTOSCI-2117 (Phase 0 of Epic JVNAUTOSCI-2112) introduces six predicates that
bind a tool concept (subject) to a structured hint body (object) describing how
the tool's output should be summarised, classified, signal-extracted, presented,
rendered, or followed-up.

These predicates are authored as instances of ``#V#predicate`` (per AGENTS.md
section 6.7 -- predicates as instances, not types). Hint bodies live in text
relations attached to a tool concept under the matching predicate. Consuming
code resolves the hint body via ``text_value_service.get_texts_for_concept``.

This module exists so that bootstrap scripts, the generic Python primitive
(``tool_result_hints.extract_signals_from_tool_result``), the synthesiser
context-prep workflow action, and tests all share a single source of truth for
these identifiers. Adding a new hint kind: extend ``OUTPUT_HINT_PREDICATE_IDS``
and update the bootstrap script.
"""

from __future__ import annotations

from typing import Final


OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID: Final[str] = "#V#output_item_summary_hint"
"""Predicate binding a tool concept to a per-item summary-shaping hint body."""

OUTPUT_ITEM_CLASSIFICATION_HINT_PREDICATE_ID: Final[str] = (
    "#V#output_item_classification_hint"
)
"""Predicate binding a tool concept to a per-item classification hint body."""

OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID: Final[str] = (
    "#V#output_item_signal_extraction_hint"
)
"""Predicate binding a tool concept to a structured signal-extraction hint body.

Consumed by ``tool_result_hints.extract_signals_from_tool_result``.
"""

OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID: Final[str] = (
    "#V#output_collection_presentation_hint"
)
"""Predicate binding a tool concept to a collection-level presentation hint body.

Should reference the per-item summary hint by predicate rather than duplicating
its body.
"""

OUTPUT_UI_RENDERING_HINT_PREDICATE_ID: Final[str] = "#V#output_ui_rendering_hint"
"""Predicate binding a tool concept to a UI-rendering hint body."""

OUTPUT_FOLLOWUP_HINT_PREDICATE_ID: Final[str] = "#V#output_followup_hint"
"""Predicate binding a tool concept to a follow-up suggestion hint body."""


OUTPUT_HINT_PREDICATE_IDS: Final[tuple[str, ...]] = (
    OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
    OUTPUT_ITEM_CLASSIFICATION_HINT_PREDICATE_ID,
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
    OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
    OUTPUT_UI_RENDERING_HINT_PREDICATE_ID,
    OUTPUT_FOLLOWUP_HINT_PREDICATE_ID,
)


__all__ = [
    "OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID",
    "OUTPUT_ITEM_CLASSIFICATION_HINT_PREDICATE_ID",
    "OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID",
    "OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID",
    "OUTPUT_UI_RENDERING_HINT_PREDICATE_ID",
    "OUTPUT_FOLLOWUP_HINT_PREDICATE_ID",
    "OUTPUT_HINT_PREDICATE_IDS",
]
