"""Prompt template resolution backed by Vontology text relations.

This module standardises how prompts are fetched from concept-linked text
fragments and rendered with contextual variables so they can be tuned without
code changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.backend.services.text_value_service import get_texts_for_concept


_PREFERRED_PREDICATE_ALIASES: Tuple[Tuple[str, ...], ...] = (
    ("hasContent", "#V#hasContent"),
    ("hasDefinition", "#V#hasDefinition"),
    ("hasDescription", "#V#hasDescription"),
)


@dataclass(frozen=True)
class PromptFragment:
    prompt_id: str
    text: str


@dataclass(frozen=True)
class RenderedPrompt:
    prompt_id: Optional[str]
    text: str
    variables: Mapping[str, Any]
    truncated: bool = False


def _detect_variables(template: str) -> List[str]:
    if not isinstance(template, str):
        return []
    return list({m.group(1) for m in re.finditer(r"{([A-Za-z0-9_]+)}", template)})


def _render_template(template: str, variables: Mapping[str, Any]) -> str:
    detected = _detect_variables(template)
    missing = [key for key in detected if key not in variables]
    if missing:
        raise ValueError(f"Missing template variables: {', '.join(missing)}")

    rendered = template
    for key in detected:
        rendered = rendered.replace("{" + key + "}", str(variables.get(key, "")))
    return rendered


def _best_effort_text_for_concept(concept_id: str) -> Optional[str]:
    try:
        texts = get_texts_for_concept(concept_id)
    except Exception:
        return None

    if not isinstance(texts, list):
        return None

    for predicate_aliases in _PREFERRED_PREDICATE_ALIASES:
        for item in texts:
            if not isinstance(item, dict):
                continue
            if item.get("predicate") not in predicate_aliases:
                continue
            text_value = item.get("text")
            if isinstance(text_value, str) and text_value.strip():
                return text_value.strip()

    return None


def fetch_prompt_fragments(
    concept_ids: Sequence[str],
    *,
    max_chars: int | None = None,
) -> List[PromptFragment]:
    fragments: List[PromptFragment] = []
    for concept_id in concept_ids:
        if not isinstance(concept_id, str) or not concept_id.strip():
            continue
        concept_id = concept_id.strip()
        text = _best_effort_text_for_concept(concept_id)
        if not text:
            continue
        if isinstance(max_chars, int) and max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"
        fragments.append(PromptFragment(prompt_id=concept_id, text=text))
    return fragments


class PromptTemplateService:
    """Resolve and render prompts from Vontology-managed text fragments."""

    def __init__(self, *, default_max_chars: int = 16000) -> None:
        self._default_max_chars = max(1000, int(default_max_chars))

    def resolve_prompt_text(
        self,
        concept_ids: Iterable[str],
        *,
        fallback: str | None = None,
        max_chars: int | None = None,
    ) -> tuple[Optional[str], Optional[str]]:
        fragments = fetch_prompt_fragments(
            list(concept_ids), max_chars=max_chars or self._default_max_chars
        )
        if fragments:
            fragment = fragments[0]
            return fragment.prompt_id, fragment.text
        if isinstance(fallback, str) and fallback.strip():
            text = fallback.strip()
            if isinstance(max_chars, int) and max_chars > 0 and len(text) > max_chars:
                text = (
                    text[:max_chars]
                    + f"\n... [truncated {len(text) - max_chars} chars]"
                )
            return None, text
        return None, None

    def render_prompt(
        self,
        concept_ids: Iterable[str],
        *,
        variables: Mapping[str, Any] | None = None,
        fallback: str | None = None,
        max_chars: int | None = None,
    ) -> Optional[RenderedPrompt]:
        prompt_id, prompt_text = self.resolve_prompt_text(
            concept_ids, fallback=fallback, max_chars=max_chars
        )
        if not prompt_text:
            return None
        variables = variables or {}
        rendered = _render_template(prompt_text, variables)

        truncated = False
        limit = max_chars or self._default_max_chars
        if len(rendered) > limit:
            rendered = (
                rendered[:limit] + f"\n... [truncated {len(rendered) - limit} chars]"
            )
            truncated = True

        return RenderedPrompt(
            prompt_id=prompt_id,
            text=rendered,
            variables=dict(variables),
            truncated=truncated,
        )
