"""Workflow selector that routes chat turns to workflow definitions.

JVNAUTOSCI-1424 Phase 2: RAG-first workflow routing.  The selector
receives candidate workflows from the capability index (including
built-in workflows like chat_assistant and tool_calling on equal footing
with Vontology-authored workflows) and asks an LLM ranker to choose the
best candidate for the user's request.

JVNAUTOSCI-1424 Phase 3: Model-based selection.  The selector now
requests structured JSON output from the LLM ranker, extracting a
confidence score (0.0–1.0) and reasoning trace alongside the workflow
selection.  This decouples precision (model-based selection) from recall
(RAG candidate retrieval) and lays the groundwork for Phase 4 RL
optimisation by recording experience tuples.

The static verdict taxonomy (plain_response, tool_seeking, summarisation,
narration) is retired.  All workflows — built-in and Vontology — compete
as RAG-retrieved candidates.

Legacy compatibility: if ``verdict_mapping`` is provided at construction,
the old static-verdict path is used so existing code can transition
incrementally.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from src.backend.services.prompt_template_service import PromptTemplateService

from .workflow_registry import WorkflowRegistry


# -----------------------------------------------------------------------
# RAG-first ranker prompt (Phase 2 default)
# -----------------------------------------------------------------------

_DEFAULT_RANKER_PROMPT = (
    "You are a workflow router.  Given the user's request and a set of "
    "candidate workflows, select the single best workflow.\n\n"
    "Return a JSON object with exactly these fields:\n"
    '- "workflow_id": the concept_id of the best workflow '
    "(e.g. #V#tool_calling_workflow)\n"
    '- "confidence": a float between 0.0 and 1.0 indicating selection '
    "confidence\n"
    '- "reasoning": a brief explanation (1-2 sentences) of why this '
    "workflow fits\n\n"
    "Rules:\n"
    "- Choose the workflow whose capabilities best match the user's intent.\n"
    "- If the request requires tools, data retrieval, file operations, API "
    "calls, or knowledge-base mutations, prefer a tool-calling or "
    "specialised workflow.\n"
    "- If the request is a simple greeting, question, or acknowledgement "
    "that needs no external data, choose the chat assistant workflow.\n"
    "- Canonical artefact identifiers and URLs (arXiv IDs, DOIs, file "
    "references) usually require a tool-calling or specialised workflow.\n"
    "- Set confidence to 1.0 when the match is unambiguous, lower when "
    "multiple workflows could apply.\n\n"
    "User request:\n{turn_text}\n\n"
    "Candidate workflows:\n{candidate_list}\n"
)

# -----------------------------------------------------------------------
# Legacy static-verdict prompt (deprecated, kept for transitional use)
# -----------------------------------------------------------------------

_LEGACY_CLASSIFIER_PROMPT = (
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

_LEGACY_DISCOVERY_SUFFIX = (
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
    confidence_score: float = 0.0
    reasoning: str = ""


@dataclass(frozen=True)
class WorkflowSelectionPrompt:
    prompt_id: Optional[str]
    prompt_text: str
    discovered_workflow_ids: tuple[str, ...] = ()


class WorkflowSelector:
    """Select a workflow for a chat turn.

    JVNAUTOSCI-1424 Phase 2: RAG-first routing.  When ``verdict_mapping``
    is None (the default), the selector operates in RAG-first mode —
    all candidate workflows (built-in + Vontology) are presented to the
    LLM ranker on equal footing.  When ``verdict_mapping`` is provided,
    the legacy static-verdict path is used for backward compatibility.
    """

    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        prompt_service: PromptTemplateService,
        verdict_mapping: Mapping[str, str] | None = None,
        default_workflow_id: str = "#V#chat_assistant_workflow",
        classifier_prompt_ids: Sequence[str] = ("#V#chat_turn_classifier_prompt",),
        fallback_prompt: str | None = None,
    ) -> None:
        self._registry = registry
        self._prompt_service = prompt_service
        self._verdict_mapping = dict(verdict_mapping) if verdict_mapping else None
        self._default_workflow_id = default_workflow_id
        self._classifier_prompt_ids = tuple(classifier_prompt_ids)
        self._fallback_prompt = fallback_prompt or (
            _LEGACY_CLASSIFIER_PROMPT if self._verdict_mapping else _DEFAULT_RANKER_PROMPT
        )
        self._rag_first = self._verdict_mapping is None

    # Legacy verdicts kept for backward-compat extraction.
    _LEGACY_VERDICTS = (
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
    _JSON_CONFIDENCE_KEYS = ("confidence", "confidence_score", "score")
    _JSON_REASONING_KEYS = ("reasoning", "reason", "explanation", "rationale")
    _CANDIDATE_BOUNDARY_STRIP = " \t\r\n`'\".,;:!?()[]{}<>"

    @property
    def rag_first(self) -> bool:
        """True when operating in RAG-first mode (no static verdict taxonomy)."""
        return self._rag_first

    def enabled(self) -> bool:
        """Check whether the workflow selector is enabled.

        JVNAUTOSCI-825: Defaults to ON.  The selector is the primary
        routing mechanism for chat turns.  Disable with
        VON_CHAT_WORKFLOW_SELECTOR_ENABLED=0 to fall back to the legacy
        always-tool-calling path.
        """
        value = os.getenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "1").strip().lower()
        return value not in {"0", "false", "off"}

    # ------------------------------------------------------------------
    # High-level API
    # ------------------------------------------------------------------

    def select_workflow(
        self,
        *,
        llm_client: Any,
        model: str | None,
        turn_text: str,
        discovered_workflows: Sequence[Mapping[str, Any]] | None = None,
    ) -> WorkflowSelection:
        """Select a workflow for the current turn."""
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

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def prepare_selection_prompt(
        self,
        *,
        turn_text: str,
        discovered_workflows: Sequence[Mapping[str, Any]] | None = None,
    ) -> WorkflowSelectionPrompt:
        """Build the selection prompt.

        In RAG-first mode, *discovered_workflows* contains ALL candidates
        (including built-in workflows) from the capability index.  The
        prompt lists every candidate so the LLM ranker can choose the
        best one.

        In legacy mode, the prompt uses the static-verdict classifier
        with discovered workflows as an optional addendum.
        """
        if self._rag_first:
            return self._prepare_rag_first_prompt(
                turn_text=turn_text,
                candidate_workflows=discovered_workflows,
            )
        return self._prepare_legacy_prompt(
            turn_text=turn_text,
            discovered_workflows=discovered_workflows,
        )

    # ------------------------------------------------------------------
    # RAG-first prompt (Phase 2)
    # ------------------------------------------------------------------

    def _prepare_rag_first_prompt(
        self,
        *,
        turn_text: str,
        candidate_workflows: Sequence[Mapping[str, Any]] | None = None,
    ) -> WorkflowSelectionPrompt:
        """Build the RAG-first ranker prompt from candidate workflows."""
        candidate_ids: list[str] = []
        candidate_lines: list[str] = []

        if candidate_workflows:
            for wf in candidate_workflows:
                cid = str(wf.get("concept_id") or "").strip()
                if not cid:
                    continue
                candidate_ids.append(cid)
                name = wf.get("name", cid)
                desc = wf.get("description", "")
                entry = f"- {cid}: {name}"
                if desc:
                    entry += f" — {desc}"
                candidate_lines.append(entry)

        # If no candidates were provided (e.g. empty capability index),
        # inject the default workflow as the sole candidate.
        if not candidate_ids:
            candidate_ids.append(self._default_workflow_id)
            candidate_lines.append(
                f"- {self._default_workflow_id}: Default workflow"
            )

        candidate_list = "\n".join(candidate_lines)

        # Try Vontology prompt template first, then fall back.
        prompt = self._prompt_service.render_prompt(
            self._classifier_prompt_ids,
            variables={"turn_text": turn_text, "candidate_list": candidate_list},
            fallback=self._fallback_prompt,
            max_chars=6000,
        )
        prompt_id = prompt.prompt_id if prompt else None
        prompt_text = prompt.text if prompt else self._fallback_prompt

        # If the rendered prompt doesn't contain the candidate list
        # (e.g. because the Vontology template doesn't have the
        # {candidate_list} variable), append it.
        if "{candidate_list}" in prompt_text:
            prompt_text = prompt_text.replace("{candidate_list}", candidate_list)
        elif candidate_list and candidate_list not in prompt_text:
            prompt_text = _DEFAULT_RANKER_PROMPT.format(
                turn_text=turn_text,
                candidate_list=candidate_list,
            )
            prompt_id = None  # Using fallback ranker prompt.

        return WorkflowSelectionPrompt(
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            discovered_workflow_ids=tuple(candidate_ids),
        )

    # ------------------------------------------------------------------
    # Legacy prompt (backward compatibility)
    # ------------------------------------------------------------------

    def _prepare_legacy_prompt(
        self,
        *,
        turn_text: str,
        discovered_workflows: Sequence[Mapping[str, Any]] | None = None,
    ) -> WorkflowSelectionPrompt:
        """Build the legacy static-verdict classifier prompt."""
        prompt = self._prompt_service.render_prompt(
            self._classifier_prompt_ids,
            variables={"turn_text": turn_text},
            fallback=self._fallback_prompt,
            max_chars=4000,
        )
        prompt_id = prompt.prompt_id if prompt else None
        prompt_text = prompt.text if prompt else self._fallback_prompt

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
                prompt_text += _LEGACY_DISCOVERY_SUFFIX.format(
                    discovered_workflows="\n".join(lines)
                )

        return WorkflowSelectionPrompt(
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            discovered_workflow_ids=tuple(discovered_ids),
        )

    # ------------------------------------------------------------------
    # Selection resolution
    # ------------------------------------------------------------------

    def resolve_selection(
        self,
        *,
        raw_response: Any,
        prompt_id: Optional[str],
        prompt_used: str | None,
        discovered_workflow_ids: Sequence[str] = (),
    ) -> WorkflowSelection:
        """Resolve a raw LLM response into a workflow selection.

        In RAG-first mode, *discovered_workflow_ids* contains ALL
        candidate workflow_ids.  The response is matched against them.

        In legacy mode, the response is matched against static verdicts
        first, then discovered workflow IDs.
        """
        if self._rag_first:
            return self._resolve_rag_first(
                raw_response=raw_response,
                prompt_id=prompt_id,
                prompt_used=prompt_used,
                candidate_workflow_ids=discovered_workflow_ids,
            )
        return self._resolve_legacy(
            raw_response=raw_response,
            prompt_id=prompt_id,
            prompt_used=prompt_used,
            discovered_workflow_ids=discovered_workflow_ids,
        )

    # ------------------------------------------------------------------
    # RAG-first resolution (Phase 2 + Phase 3 structured output)
    # ------------------------------------------------------------------

    def _resolve_rag_first(
        self,
        *,
        raw_response: Any,
        prompt_id: Optional[str],
        prompt_used: str | None,
        candidate_workflow_ids: Sequence[str] = (),
    ) -> WorkflowSelection:
        """Resolve selection from RAG candidate list.

        Phase 3: extracts confidence score and reasoning trace from
        structured JSON responses.  Falls back gracefully when the LLM
        returns a plain concept_id or unstructured text.
        """
        candidate_ids = tuple(
            item for item in candidate_workflow_ids
            if isinstance(item, str) and item
        )
        candidate_lookup = {item.lower(): item for item in candidate_ids}

        # Phase 3: attempt structured extraction first.
        structured = self._parse_structured_selection(
            raw_response=raw_response,
        )
        confidence_score = structured.get("confidence", 0.0)
        reasoning = structured.get("reasoning", "")

        label = self._extract_candidate_label(
            raw_response=raw_response,
            discovered_workflow_ids=candidate_ids,
        )

        # Match against candidate workflow IDs.
        label_lower = label.lower()
        matched_id = candidate_lookup.get(label_lower)

        if matched_id:
            workflow_id = matched_id
            verdict = "rag_selected"
        else:
            # Fallback: try to find a candidate in the raw text.
            raw_text = str(raw_response or "").lower()
            for cid in candidate_ids:
                if cid.lower() in raw_text:
                    workflow_id = cid
                    verdict = "rag_selected"
                    break
            else:
                # Ultimate fallback to default workflow.
                workflow_id = self._default_workflow_id
                verdict = "rag_default"
                # Lower confidence for fallback selections.
                if confidence_score > 0.0:
                    confidence_score = min(confidence_score, 0.3)

        return WorkflowSelection(
            workflow_id=workflow_id,
            verdict=verdict,
            prompt_id=prompt_id,
            prompt_used=prompt_used,
            raw_response=str(raw_response or ""),
            discovered_workflow_ids=candidate_ids,
            confidence_score=confidence_score,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Legacy resolution (backward compatibility)
    # ------------------------------------------------------------------

    def _resolve_legacy(
        self,
        *,
        raw_response: Any,
        prompt_id: Optional[str],
        prompt_used: str | None,
        discovered_workflow_ids: Sequence[str] = (),
    ) -> WorkflowSelection:
        """Resolve selection using static verdict mapping (legacy path)."""
        verdict = self._extract_candidate_label(
            raw_response=raw_response,
            discovered_workflow_ids=discovered_workflow_ids,
        )
        verdict_lookup = verdict.lower()
        resolved_verdict = verdict
        discovered_ids = tuple(
            item for item in discovered_workflow_ids
            if isinstance(item, str) and item
        )
        discovered_lookup = {item.lower(): item for item in discovered_ids}

        mapping = self._verdict_mapping or {}

        # 1. Check static verdict mapping.
        workflow_id = mapping.get(verdict_lookup)

        # 2. Check if the verdict is a discovered workflow concept_id.
        discovered_match = discovered_lookup.get(verdict_lookup)
        if not workflow_id and discovered_match:
            workflow_id = discovered_match
            resolved_verdict = discovered_match

        # 3. Fallback to plain_response mapping.
        if not workflow_id:
            workflow_id = mapping.get("plain_response") or self._default_workflow_id
            resolved_verdict = "plain_response"

        if not isinstance(workflow_id, str):
            workflow_id = ""

        # Validate workflow_id is in the registry.
        if workflow_id and workflow_id not in self._registry.all_workflow_ids():
            if workflow_id not in discovered_ids:
                fallback_id = mapping.get("plain_response") or self._default_workflow_id
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

    # ------------------------------------------------------------------
    # Structured selection parsing (Phase 3)
    # ------------------------------------------------------------------

    @classmethod
    def _parse_structured_selection(
        cls,
        *,
        raw_response: Any,
    ) -> dict[str, Any]:
        """Extract confidence and reasoning from a structured JSON response.

        Returns a dict with keys ``confidence`` (float 0.0–1.0) and
        ``reasoning`` (str).  Falls back to empty defaults when the
        response is not valid JSON or lacks the expected fields.
        """
        raw_text = str(raw_response or "").strip()
        if not raw_text:
            return {"confidence": 0.0, "reasoning": ""}

        try:
            parsed = json.loads(raw_text)
        except Exception:
            return {"confidence": 0.0, "reasoning": ""}

        if not isinstance(parsed, Mapping):
            return {"confidence": 0.0, "reasoning": ""}

        confidence = 0.0
        for key in cls._JSON_CONFIDENCE_KEYS:
            raw_conf = parsed.get(key)
            if raw_conf is not None:
                try:
                    confidence = max(0.0, min(1.0, float(raw_conf)))
                except (TypeError, ValueError):
                    pass
                break

        reasoning = ""
        for key in cls._JSON_REASONING_KEYS:
            raw_reason = parsed.get(key)
            if isinstance(raw_reason, str) and raw_reason.strip():
                reasoning = raw_reason.strip()
                break

        return {"confidence": confidence, "reasoning": reasoning}

    # ------------------------------------------------------------------
    # Label extraction (shared by both modes)
    # ------------------------------------------------------------------

    @classmethod
    def _extract_candidate_label(
        cls,
        *,
        raw_response: Any,
        discovered_workflow_ids: Sequence[str] = (),
    ) -> str:
        raw_text = str(raw_response or "")
        discovered_ids = tuple(
            item for item in discovered_workflow_ids
            if isinstance(item, str) and item
        )
        discovered_lookup = {item.lower(): item for item in discovered_ids}

        snippets: list[str] = []
        stripped = raw_text.strip()
        if stripped:
            snippets.append(stripped)
            snippets.extend(
                line.strip()
                for line in stripped.splitlines()
                if isinstance(line, str)
            )

        json_candidate = cls._extract_json_candidate(raw_text)
        if json_candidate:
            snippets.insert(0, json_candidate)

        for snippet in snippets:
            normalised = cls._normalise_candidate(snippet)
            # Check legacy verdicts (for backward-compat extraction).
            if normalised in cls._LEGACY_VERDICTS:
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
        """Extract a legacy static verdict from text.  Kept for compatibility."""
        best_match: tuple[int, str] | None = None
        for verdict in cls._LEGACY_VERDICTS:
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
