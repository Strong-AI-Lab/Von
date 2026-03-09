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

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from src.backend.services.prompt_template_service import PromptTemplateService

from .workflow_registry import WorkflowRegistry


_DEFAULT_CLASSIFIER_PROMPT = (
    "You are a workflow router for the next assistant turn.\n"
    "Return exactly one routing label and nothing else.\n"
    "Allowed labels: plain_response, tool_seeking, summarisation, narration.\n"
    "Do not return prose, JSON, punctuation, explanations, code fences, or more than one label.\n"
    "Choose tool_seeking whenever satisfying the request likely requires tools, MCP calls, retrieval, downloads, reads, searches, file access, or additive knowledge-base mutations.\n"
    "Canonical artefact identifiers and URLs usually imply tool_seeking when acting on the artefact is needed, including bare arXiv URLs or arXiv IDs.\n"
    "Choose plain_response only for direct conversational replies that do not need tools.\n"
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


@dataclass(frozen=True)
class WorkflowSelectionPrompt:
    prompt_id: Optional[str]
    prompt_text: str
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

    _STATIC_SELECTOR_VERDICTS = (
        "plain_response",
        "tool_seeking",
        "summarisation",
        "narration",
    )
    _JSON_SELECTION_KEYS = (
        "workflow_id",
        "workflow",
        "verdict",
        "label",
        "route",
        "selection",
    )
    _CANDIDATE_BOUNDARY_STRIP = " \t\r\n`'\".,;:!?()[]{}<>"

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
        selection_prompt = self.prepare_selection_prompt(
            turn_text=turn_text,
            discovered_workflows=discovered_workflows,
        )
        response = llm_client.generate(
            prompt="Select workflow",
            context=[{"role": "system", "content": selection_prompt.prompt_text}],
            model=model,
        )
        return self.resolve_selection(
            raw_response=response,
            prompt_id=selection_prompt.prompt_id,
            prompt_used=selection_prompt.prompt_text,
            discovered_workflow_ids=selection_prompt.discovered_workflow_ids,
        )

    def prepare_selection_prompt(
        self,
        *,
        turn_text: str,
        discovered_workflows: Sequence[Mapping[str, Any]] | None = None,
    ) -> WorkflowSelectionPrompt:
        """Build the classifier prompt and discovered-workflow context."""
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

        return WorkflowSelectionPrompt(
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            discovered_workflow_ids=tuple(discovered_ids),
        )

    def resolve_selection(
        self,
        *,
        raw_response: Any,
        prompt_id: Optional[str],
        prompt_used: str | None,
        discovered_workflow_ids: Sequence[str] = (),
    ) -> WorkflowSelection:
        """Resolve a raw classifier response into a workflow selection."""
        verdict = self._extract_candidate_label(
            raw_response=raw_response,
            discovered_workflow_ids=discovered_workflow_ids,
        )
        verdict_lookup = verdict.lower()
        resolved_verdict = verdict
        discovered_ids = tuple(
            item for item in discovered_workflow_ids if isinstance(item, str) and item
        )
        discovered_lookup = {item.lower(): item for item in discovered_ids}

        # Resolve verdict → workflow_id.
        # 1. Check static verdict mapping.
        workflow_id = self._verdict_mapping.get(verdict_lookup)

        # 2. Check if the verdict is a discovered workflow concept_id.
        discovered_match = discovered_lookup.get(verdict_lookup)
        if not workflow_id and discovered_match:
            workflow_id = discovered_match
            resolved_verdict = discovered_match

        # 3. Fallback to plain_response mapping.
        if not workflow_id:
            workflow_id = self._verdict_mapping.get("plain_response")
            resolved_verdict = "plain_response"

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
                resolved_verdict = "plain_response"

        return WorkflowSelection(
            workflow_id=workflow_id or "",
            verdict=resolved_verdict or "",
            prompt_id=prompt_id,
            prompt_used=prompt_used,
            raw_response=str(raw_response or ""),
            discovered_workflow_ids=tuple(discovered_ids),
        )

    @classmethod
    def _extract_candidate_label(
        cls,
        *,
        raw_response: Any,
        discovered_workflow_ids: Sequence[str] = (),
    ) -> str:
        raw_text = str(raw_response or "")
        discovered_ids = tuple(
            item for item in discovered_workflow_ids if isinstance(item, str) and item
        )
        discovered_lookup = {item.lower(): item for item in discovered_ids}

        snippets: list[str] = []
        stripped = raw_text.strip()
        if stripped:
            snippets.append(stripped)
            snippets.extend(
                line.strip() for line in stripped.splitlines() if isinstance(line, str)
            )

        json_candidate = cls._extract_json_candidate(raw_text)
        if json_candidate:
            snippets.insert(0, json_candidate)

        for snippet in snippets:
            normalised = cls._normalise_candidate(snippet)
            if normalised in cls._STATIC_SELECTOR_VERDICTS:
                return normalised
            discovered_match = discovered_lookup.get(normalised)
            if discovered_match:
                return discovered_match

        lowered = raw_text.lower()
        for workflow_id in discovered_ids:
            if workflow_id.lower() in lowered:
                return workflow_id

        static_match = cls._extract_static_verdict_from_text(lowered)
        if static_match:
            return static_match

        return cls._normalise_candidate(raw_text)

    @classmethod
    def _extract_json_candidate(cls, raw_text: str) -> str | None:
        stripped = raw_text.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except Exception:
            return None
        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, Mapping):
            for key in cls._JSON_SELECTION_KEYS:
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return None

    @classmethod
    def _extract_static_verdict_from_text(cls, lowered_text: str) -> str | None:
        best_match: tuple[int, str] | None = None
        for verdict in cls._STATIC_SELECTOR_VERDICTS:
            match = re.search(rf"\b{re.escape(verdict)}\b", lowered_text)
            if not match:
                continue
            position = match.start()
            if best_match is None or position < best_match[0]:
                best_match = (position, verdict)
        return best_match[1] if best_match is not None else None

    @classmethod
    def _normalise_candidate(cls, value: str) -> str:
        return str(value or "").strip().strip(cls._CANDIDATE_BOUNDARY_STRIP).lower()
