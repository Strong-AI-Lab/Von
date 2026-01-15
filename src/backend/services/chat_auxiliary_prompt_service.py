"""Chat auxiliary prompt retrieval.

JVNAUTOSCI-797: Allow chat to include user-specific system prompts stored in the
Vontology as instances of `#V#von_llm_prompt` linked to the current user.

The UI provides a selected user concept ID, but the backend must only apply
prompts for the effective authenticated user.

This module keeps the logic small and testable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging
import os

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.services.concept_service import get_concept_by_concept_id
from src.backend.services.text_value_service import get_texts_for_concept

_VON_CHAT_BEHAVIOUR_PROMPT_TYPE = "#V#von_chat_behaviour_prompt"
# Legacy/alternate ID (US spelling) observed in stored concepts.
_VON_CHAT_BEHAVIOR_PROMPT_TYPE = "#V#von_chat_behavior_prompt"
_VON_CHAT_NARRATION_PROMPT_TYPE = "#V#von_chat_narration_prompt"
_LEGACY_VON_LLM_PROMPT_TYPE = "#V#von_llm_prompt"
_SPECIFIC_TO_USER_PREDICATE_CANDIDATES = (
    "#V#specific_to_von_user",
    "specific_to_von_user",
    "#V#specific_to_user",
    "specific_to_user",
)


logger = logging.getLogger(__name__)


def _debug_user_prompt_logging_enabled() -> bool:
    value = os.getenv("VON_DEBUG_USER_PROMPT_LOADING", "")
    value = value.strip().lower()
    return value in {"1", "true", "yes", "on"}


def _normalise_predicate_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("#V#"):
        return value[3:]
    return value


def _get_prompt_content_for_concept(concept_id: str) -> Optional[str]:
    if not isinstance(concept_id, str) or not concept_id.strip():
        return None

    concept_id = concept_id.strip()

    try:
        concept = get_concept_by_concept_id(concept_id)
    except Exception:
        concept = None

    if isinstance(concept, dict):
        content = concept.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    try:
        texts = get_texts_for_concept(concept_id)
    except Exception:
        texts = None

    if not isinstance(texts, list):
        return None

    content_candidates: List[Dict[str, Any]] = []
    for text in texts:
        if not isinstance(text, dict):
            continue
        predicate = _normalise_predicate_id(text.get("predicate"))
        if predicate not in {"hasContent", "hasDescription"}:
            continue
        text_value = text.get("text")
        if not isinstance(text_value, str):
            continue
        if not text_value.strip():
            continue
        content_candidates.append(text)

    if not content_candidates:
        return None

    def _sort_key(item: Dict[str, Any]) -> tuple[int, int, str]:
        predicate = _normalise_predicate_id(item.get("predicate"))
        lang = item.get("lang")
        text_value_id = item.get("text_value_id")

        predicate_rank = 0 if predicate == "hasContent" else 1
        lang_rank = 0 if lang in {"en-NZ", "en"} else 1
        text_id = text_value_id if isinstance(text_value_id, str) else ""

        return (predicate_rank, lang_rank, text_id)

    content_candidates.sort(key=_sort_key)

    joined = "\n\n".join(candidate["text"].strip() for candidate in content_candidates)
    joined = joined.strip()
    return joined if joined else None


def build_user_specific_system_prompt(
    user_concept_id: str,
    *,
    prompt_types: Sequence[str] | None = None,
) -> Optional[str]:
    """Build a system prompt string for the given authenticated user.

    We locate concepts that:
    - are instances of `#V#von_chat_behaviour_prompt` (or legacy `#V#von_llm_prompt`), and
    - are linked to the user via `#V#specific_to_von_user`.

    If multiple prompts exist, we concatenate them in a deterministic order.
    """

    fragments = get_user_specific_prompt_fragments(
        user_concept_id, prompt_types=prompt_types
    )
    prompt_texts = [
        fragment["content"] for fragment in fragments if fragment.get("content")
    ]
    if not prompt_texts:
        return None

    return "\n\n".join(prompt_texts)


def _normalise_prompt_types(
    prompt_types: Sequence[str] | None,
) -> Tuple[str, ...]:
    if prompt_types is None:
        # Default behaviour: apply the user's chat behaviour prompt. We keep
        # the legacy type as a fallback so restored data continues to work.
        return (
            _VON_CHAT_BEHAVIOUR_PROMPT_TYPE,
            _VON_CHAT_BEHAVIOR_PROMPT_TYPE,
            _LEGACY_VON_LLM_PROMPT_TYPE,
        )

    normalised: List[str] = []
    for value in prompt_types:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value:
            continue
        if value not in normalised:
            normalised.append(value)

    return tuple(normalised)


def get_user_specific_prompt_fragments(
    user_concept_id: str,
    *,
    prompt_types: Sequence[str] | None = None,
) -> List[Dict[str, Any]]:
    """Return the list of Vontology prompt fragments for a user.

    Each fragment is a dict with:
    - concept_id: str
    - content: str

    The list is returned in deterministic order.
    """

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return []

    user_concept_id = user_concept_id.strip()

    debug_enabled = _debug_user_prompt_logging_enabled()

    type_candidates = _normalise_prompt_types(prompt_types)
    if not type_candidates:
        return []

    relationship_fields = [
        f"relationships.{predicate}"
        for predicate in _SPECIFIC_TO_USER_PREDICATE_CANDIDATES
        if isinstance(predicate, str) and predicate.strip()
    ]

    projection = {"concept_id": 1, "name": 1}
    if debug_enabled:
        for field in relationship_fields:
            projection[field] = 1

    prompt_candidates = ConceptsRepository.find(
        {
            "relationships.is_an_instance_of": {"$in": list(type_candidates)},
            "$or": [{field: user_concept_id} for field in relationship_fields],
        },
        projection=projection,
        sort=[("concept_id", 1)],
        limit=50,
    )

    prompt_ids: List[str] = []
    for doc in prompt_candidates:
        concept_id = doc.get("concept_id") if isinstance(doc, dict) else None
        if isinstance(concept_id, str) and concept_id.startswith("#V#"):
            prompt_ids.append(concept_id)

            if debug_enabled:
                relationships = (
                    doc.get("relationships") if isinstance(doc, dict) else None
                )
                match_predicate = None
                if isinstance(relationships, dict):
                    for predicate in _SPECIFIC_TO_USER_PREDICATE_CANDIDATES:
                        values = relationships.get(predicate)
                        if isinstance(values, list) and user_concept_id in values:
                            match_predicate = predicate
                            break
                        if isinstance(values, str) and values == user_concept_id:
                            match_predicate = predicate
                            break

                logger.debug(
                    "User prompt concept loaded: concept_id=%s user_concept_id=%s match_predicate=%s",
                    concept_id,
                    user_concept_id,
                    match_predicate or "(unknown)",
                )

    if not prompt_ids:
        return []

    fragments: List[Dict[str, Any]] = []
    for concept_id in prompt_ids:
        content = _get_prompt_content_for_concept(concept_id)
        if isinstance(content, str) and content.strip():
            fragments.append({"concept_id": concept_id, "content": content.strip()})

    return fragments
