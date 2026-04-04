"""Compatibility wrapper for semantic paper recommendation materialisation.

The original lexical/English-specific ranking code has been retired. This
module keeps the established public entry point while delegating to the new
semantic materialisation path.
"""

from __future__ import annotations

from typing import Any, Sequence

from .paper_recommendation_constants import PAPER_RECOMMENDATION_POLICY_VERSION
from .paper_recommendation_materialisation_service import (
    materialise_paper_recommendations_for_subject,
)


def build_paper_recommendations(
    *,
    user_concept_id: str,
    candidate_paper_concept_ids: Sequence[str],
    max_results: int = 10,
    include_all_candidates: bool = False,
) -> dict[str, Any]:
    """Build paper recommendations for one subject via semantic evaluation."""

    return materialise_paper_recommendations_for_subject(
        subject_concept_id=user_concept_id,
        candidate_paper_concept_ids=candidate_paper_concept_ids,
        max_results=max_results,
        include_all_candidates=include_all_candidates,
        trigger_source="build_paper_recommendations",
    )


__all__ = [
    "PAPER_RECOMMENDATION_POLICY_VERSION",
    "build_paper_recommendations",
]
