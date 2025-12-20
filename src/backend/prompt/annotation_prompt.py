import time
import logging
from typing import Optional, Dict, Any

from ..services.text_value_service import get_texts_for_concept
from ..models.text_value_models import RelationPredicate

logger = logging.getLogger(__name__)


class AnnotationPromptBuilder:
    """Central helper for constructing annotation extraction instructions.

    Canonical, relation-only retrieval (NO legacy top-level/preserved fallbacks) with
    strict predicate precedence:
      1. hasContent
      2. hasDescription

    If both predicates absent (or empty), returns placeholder text instructing the
    operator to create the prompt concept / add content. Truncates resulting
    instruction to a safe upper bound (4000 chars) leaving room for appended text.
    """

    def __init__(self, concept_id: str, ttl_sec: int = 300):
        self.concept_id = concept_id
        self.ttl_sec = ttl_sec
        self._cache: Dict[str, Any] = {
            "text": None,
            "ts": 0.0,
            "source_predicate": None,
        }

    def invalidate(self):
        self._cache = {"text": None, "ts": 0.0, "source_predicate": None}

    def _fetch_relation_text(self, predicate: str) -> Optional[str]:
        try:
            rels = get_texts_for_concept(self.concept_id, predicate=predicate, limit=1)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(
                f"PromptBuilder relation fetch failed predicate={predicate}: {e}"
            )
            return None
        if not rels:
            return None
        txt = rels[0].get("text")
        if isinstance(txt, str):
            stripped = txt.strip()
            return stripped or None
        return None

    def get_instruction(self) -> str:
        now = time.time()
        if self._cache["text"] and now - self._cache["ts"] < self.ttl_sec:
            return self._cache["text"]  # type: ignore

        instruction: Optional[str] = None
        source_predicate: Optional[str] = None

        for predicate in (
            RelationPredicate.HAS_CONTENT,
            RelationPredicate.HAS_DESCRIPTION,
        ):
            txt = self._fetch_relation_text(predicate)
            if txt:
                instruction = txt
                source_predicate = predicate
                break

        if not instruction:
            # Provide explicit actionable placeholder
            instruction = (
                f"Prompt concept {self.concept_id} missing canonical relation text (hasContent/hasDescription). "
                f"Create a hasContent text to define extraction behaviour."
            )

        # Safe length cap
        if len(instruction) > 4000:
            instruction = instruction[:4000] + " …"

        # Cache
        self._cache["text"] = instruction
        self._cache["ts"] = now
        self._cache["source_predicate"] = source_predicate

        try:
            logger.info(
                "AnnotationPromptBuilder: concept_id=%s source=%s length=%s",
                self.concept_id,
                source_predicate or "placeholder",
                len(instruction),
            )
        except Exception:  # pragma: no cover
            pass

        return instruction

    def stats(self) -> Dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "cached": bool(self._cache.get("text")),
            "age_sec": (
                (time.time() - self._cache["ts"]) if self._cache.get("ts") else None
            ),
            "source_predicate": self._cache.get("source_predicate"),
            "ttl_sec": self.ttl_sec,
        }


__all__ = ["AnnotationPromptBuilder"]
