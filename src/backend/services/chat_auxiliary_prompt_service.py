"""Chat auxiliary prompt retrieval.

JVNAUTOSCI-797: Allow chat to include user-specific system prompts stored in the
Vontology as instances of `#V#von_llm_prompt` linked to the current user.

The UI provides a selected user concept ID, but the backend must only apply
prompts for the effective authenticated user.

This module keeps the logic small and testable.
"""

from __future__ import annotations

from typing import List, Optional

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.services.concept_service import get_concept_by_concept_id


_VON_LLM_PROMPT_TYPE = "#V#von_llm_prompt"
_SPECIFIC_TO_USER_PREDICATE = "#V#specific_to_von_user"


def build_user_specific_system_prompt(user_concept_id: str) -> Optional[str]:
    """Build a system prompt string for the given authenticated user.

    We locate concepts that:
    - are instances of `#V#von_llm_prompt`, and
    - are linked to the user via `#V#specific_to_von_user`.

    If multiple prompts exist, we concatenate them in a deterministic order.
    """

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return None

    user_concept_id = user_concept_id.strip()

    relationship_field = f"relationships.{_SPECIFIC_TO_USER_PREDICATE}"

    prompt_candidates = ConceptsRepository.find(
        {
            "relationships.is_an_instance_of": _VON_LLM_PROMPT_TYPE,
            relationship_field: user_concept_id,
        },
        projection={"concept_id": 1, "name": 1},
        sort=[("concept_id", 1)],
        limit=50,
    )

    prompt_ids: List[str] = []
    for doc in prompt_candidates:
        concept_id = doc.get("concept_id") if isinstance(doc, dict) else None
        if isinstance(concept_id, str) and concept_id.startswith("#V#"):
            prompt_ids.append(concept_id)

    if not prompt_ids:
        return None

    prompt_texts: List[str] = []
    for concept_id in prompt_ids:
        try:
            concept = get_concept_by_concept_id(concept_id)
        except Exception:
            continue

        if not isinstance(concept, dict):
            continue

        content = concept.get("content")
        if isinstance(content, str) and content.strip():
            prompt_texts.append(content.strip())

    if not prompt_texts:
        return None

    return "\n\n".join(prompt_texts)
