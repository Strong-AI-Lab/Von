"""Workflow selector that routes chat turns to workflow definitions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.backend.services.prompt_template_service import PromptTemplateService

from .workflow_registry import WorkflowRegistry


_DEFAULT_CLASSIFIER_PROMPT = (
    "You are a router that selects a workflow for the next assistant turn.\n"
    "Pick ONE of: plain_response, tool_seeking, summarisation, narration.\n"
    "Reply with only the label. Treat tool-seeking as any case where tools or MCP calls are expected.\n"
    "User+assistant turn so far:\n"
    "{turn_text}\n"
)


@dataclass(frozen=True)
class WorkflowSelection:
    workflow_id: str
    verdict: str
    prompt_id: Optional[str]
    prompt_used: str | None
    raw_response: str


class WorkflowSelector:
    """Select a workflow for a chat turn based on an LLM classifier."""

    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        prompt_service: PromptTemplateService,
        verdict_mapping: Mapping[str, str],
        classifier_prompt_ids: Sequence[str] = ("#V#chat_turn_classifier_prompt",),
        fallback_prompt: str = _DEFAULT_CLASSIFIER_PROMPT,
    ) -> None:
        self._registry = registry
        self._prompt_service = prompt_service
        self._verdict_mapping = dict(verdict_mapping)
        self._classifier_prompt_ids = tuple(classifier_prompt_ids)
        self._fallback_prompt = fallback_prompt

    def enabled(self) -> bool:
        value = os.getenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "1").strip().lower()
        return value not in {"0", "false", "off"}

    def select_workflow(
        self,
        *,
        llm_client: Any,
        model: str | None,
        turn_text: str,
    ) -> WorkflowSelection:
        prompt = self._prompt_service.render_prompt(
            self._classifier_prompt_ids,
            variables={"turn_text": turn_text},
            fallback=self._fallback_prompt,
            max_chars=4000,
        )
        prompt_id = prompt.prompt_id if prompt else None
        prompt_text = prompt.text if prompt else self._fallback_prompt

        response = llm_client.generate(
            prompt="Select workflow",
            context=[{"role": "system", "content": prompt_text}],
            model=model,
        )
        verdict = str(response or "").strip().lower()
        workflow_id = self._verdict_mapping.get(verdict) or self._verdict_mapping.get(
            "plain_response"
        )
        if workflow_id not in self._registry.all_workflow_ids():
            workflow_id = self._verdict_mapping.get("plain_response", workflow_id or "")
        return WorkflowSelection(
            workflow_id=workflow_id or "",
            verdict=verdict or "",
            prompt_id=prompt_id,
            prompt_used=prompt_text,
            raw_response=str(response or ""),
        )
