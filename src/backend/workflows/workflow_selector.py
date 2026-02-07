"""Workflow selector that routes chat turns to workflow definitions.

JVNAUTOSCI-922 Phase 1.3: The selector routes conversation turns to
workflow definitions.  It supports both static verdicts (plain_response,
tool_seeking, summarisation, narration) and dynamically discovered
Vontology workflows surfaced by ``discover_workflows_for_turn()``.

The classifier prompt is augmented with discovered workflow descriptions
so the LLM can choose a discovered workflow by its concept_id when the
user's intent matches.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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

# Appended when discovered workflows are available.
_DISCOVERY_SUFFIX = (
    "\n\n"
    "In addition to the standard verdicts, these specialised workflows are available.\n"
    "If the user's request clearly matches one, respond with its concept_id instead "
    "of a standard verdict.  Only choose a specialised workflow when the match is "
    "strong — default to tool_seeking or plain_response when uncertain.\n\n"
    "{discovered_workflows}\n"
)


@dataclass(frozen=True)
class WorkflowSelection:
    workflow_id: str
    verdict: str
    prompt_id: Optional[str]
    prompt_used: str | None
    raw_response: str
    discovered_workflow_ids: tuple[str, ...] = ()


class WorkflowSelector:
    """Select a workflow for a chat turn based on an LLM classifier.

    The selector can be augmented with ``discovered_workflows`` from
    ``discover_workflows_for_turn()``.  When present, the discovered
    workflow concept_ids are valid responses from the classifier alongside
    the static verdicts.  See JVNAUTOSCI-922 Phase 1.3.
    """

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
        """Check whether the workflow selector is enabled.

        JVNAUTOSCI-825: Defaults to ON.  The selector is the primary
        routing mechanism for chat turns — it decides whether to run
        tool-calling, narration, a discovered workflow, or a plain
        response.  Disable with VON_CHAT_WORKFLOW_SELECTOR_ENABLED=0
        to fall back to the legacy always-tool-calling path.
        """
        value = os.getenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "1").strip().lower()
        return value not in {"0", "false", "off"}

    def select_workflow(
        self,
        *,
        llm_client: Any,
        model: str | None,
        turn_text: str,
        discovered_workflows: Sequence[Mapping[str, Any]] | None = None,
    ) -> WorkflowSelection:
        """Select a workflow for the current turn.

        Args:
            llm_client: LLM client for the classifier call.
            model: Model to use for classification.
            turn_text: Combined user+assistant conversation text.
            discovered_workflows: Optional list of workflow matches from
                ``discover_workflows_for_turn()``.  Each entry should have
                ``concept_id``, ``name``, and optionally ``description``.
        """
        prompt = self._prompt_service.render_prompt(
            self._classifier_prompt_ids,
            variables={"turn_text": turn_text},
            fallback=self._fallback_prompt,
            max_chars=4000,
        )
        prompt_id = prompt.prompt_id if prompt else None
        prompt_text = prompt.text if prompt else self._fallback_prompt

        # Build set of discovered workflow concept_ids for verdict resolution.
        discovered_ids: list[str] = []
        if discovered_workflows:
            lines: list[str] = []
            for wf in discovered_workflows:
                cid = wf.get("concept_id", "")
                name = wf.get("name", cid)
                desc = wf.get("description", "")
                if not cid:
                    continue
                discovered_ids.append(cid)
                entry = f"- {cid}: {name}"
                if desc:
                    entry += f" — {desc}"
                lines.append(entry)
            if lines:
                prompt_text += _DISCOVERY_SUFFIX.format(
                    discovered_workflows="\n".join(lines)
                )

        response = llm_client.generate(
            prompt="Select workflow",
            context=[{"role": "system", "content": prompt_text}],
            model=model,
        )
        verdict = str(response or "").strip().lower()

        # Resolve verdict → workflow_id.
        # 1. Check static verdict mapping.
        workflow_id = self._verdict_mapping.get(verdict)

        # 2. Check if the verdict is a discovered workflow concept_id.
        if not workflow_id and verdict in {d.lower() for d in discovered_ids}:
            # Normalise to the original casing from the discovered list.
            for d in discovered_ids:
                if d.lower() == verdict:
                    workflow_id = d
                    break

        # 3. Fallback to plain_response mapping.
        if not workflow_id:
            workflow_id = self._verdict_mapping.get("plain_response")

        if not isinstance(workflow_id, str):
            workflow_id = ""

        # Validate workflow_id is in the registry (static or Vontology-loaded).
        if workflow_id and workflow_id not in self._registry.all_workflow_ids():
            # If it's a discovered workflow, it may not be in the registry yet
            # but is still valid — the orchestrator can attempt to load it.
            if workflow_id not in discovered_ids:
                fallback_id = self._verdict_mapping.get("plain_response")
                workflow_id = (
                    fallback_id if isinstance(fallback_id, str) else workflow_id
                )

        return WorkflowSelection(
            workflow_id=workflow_id or "",
            verdict=verdict or "",
            prompt_id=prompt_id,
            prompt_used=prompt_text,
            raw_response=str(response or ""),
            discovered_workflow_ids=tuple(discovered_ids),
        )
