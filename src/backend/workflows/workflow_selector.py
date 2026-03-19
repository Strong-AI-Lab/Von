"""Workflow selector that routes chat turns to workflow definitions.

JVNAUTOSCI-1424 Phase 2 introduced RAG-first workflow routing: the selector
receives candidate workflows from discovery/capability matching and asks an
LLM ranker to choose the single best workflow for the user's request.

JVNAUTOSCI-1424 Phase 3 added structured model output so the selector can
carry confidence and reasoning alongside the chosen workflow.

JVNAUTOSCI-1521 removes the retired static-verdict selector mode. Production
routing now has one authoritative selection path: candidate workflows in,
workflow ID out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from src.backend.services.prompt_template_service import PromptTemplateService
from src.backend.services.workflow_selection_policy_service import (
    recommend_workflow_with_policy,
)

from .workflow_registry import WorkflowRegistry


# Fail closed if the authoritative selector prompt cannot be resolved from
# Vontology or omits the required routing context variables.
SELECTOR_PROMPT_UNAVAILABLE_REASON = "selector_prompt_unavailable"
SELECTOR_PROMPT_RENDER_ERROR_REASON = "selector_prompt_render_error"
SELECTOR_PROMPT_MISSING_CANDIDATE_LIST_REASON = (
    "selector_prompt_missing_candidate_list"
)
SELECTOR_PROMPT_MISSING_TURN_TEXT_REASON = "selector_prompt_missing_turn_text"
SELECTOR_FAIL_CLOSED_SOURCE = "selector_fail_closed"


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
    selection_source: str = "selector"
    selection_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowSelectionPrompt:
    prompt_id: Optional[str]
    prompt_text: str | None
    discovered_workflow_ids: tuple[str, ...] = ()
    candidate_entries: tuple[dict[str, Any], ...] = ()
    policy_recommendation: Mapping[str, Any] = field(default_factory=dict)
    prompt_failure_reason: str | None = None
    prompt_failure_detail: str | None = None


class WorkflowSelector:
    """Select a workflow for a chat turn.

    The selector is intentionally single-path: all candidate workflows
    compete on equal footing and the selector resolves one workflow ID.
    """

    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        prompt_service: PromptTemplateService,
        default_workflow_id: str = "#V#chat_assistant_workflow",
        classifier_prompt_ids: Sequence[str] = ("#V#chat_turn_classifier_prompt",),
    ) -> None:
        self._registry = registry
        self._prompt_service = prompt_service
        self._default_workflow_id = default_workflow_id
        self._classifier_prompt_ids = tuple(classifier_prompt_ids)
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
        """Retained for backwards-compatible introspection.

        The selector is always in candidate-based workflow mode.
        """
        return True

    def enabled(self) -> bool:
        """Return whether workflow selection is enabled for routing.

        The selector is always the authoritative routing path now. The method is
        retained only so existing call sites and tests do not need a separate
        interface migration.
        """
        return True

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
        prompt_text = selection_prompt.prompt_text
        selection_policy = (
            dict(selection_prompt.policy_recommendation)
            if isinstance(selection_prompt.policy_recommendation, Mapping)
            else {}
        )
        if (
            isinstance(selection_policy.get("recommended_workflow_id"), str)
            and selection_policy.get("guidance_mode") == "direct"
        ):
            return self.resolve_policy_selection(
                workflow_id=str(selection_policy.get("recommended_workflow_id")),
                prompt_id=selection_prompt.prompt_id,
                prompt_used=selection_prompt.prompt_text,
                discovered_workflow_ids=selection_prompt.discovered_workflow_ids,
                confidence_score=float(
                    selection_policy.get("confidence_score", 0.0)
                ),
                reasoning=str(selection_policy.get("reasoning") or ""),
                selection_metadata=selection_policy,
            )
        if not prompt_text:
            return self.resolve_prompt_unavailable_selection(
                selection_prompt=selection_prompt,
            )
        response = llm_client.generate(
            prompt="Select workflow",
            context=[{"role": "system", "content": prompt_text}],
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

        *discovered_workflows* contains all currently eligible candidates
        (including built-in workflows). The prompt lists every candidate so
        the LLM ranker can choose the best one.
        """
        return self._prepare_rag_first_prompt(
            turn_text=turn_text,
            candidate_workflows=discovered_workflows,
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
        candidate_entries: list[dict[str, str]] = []

        if candidate_workflows:
            for wf in candidate_workflows:
                cid = str(wf.get("concept_id") or "").strip()
                if not cid:
                    continue
                candidate_entries.append(
                    {
                        "concept_id": cid,
                        "name": str(wf.get("name") or cid),
                        "description": str(wf.get("description") or ""),
                    }
                )

        policy_recommendation = recommend_workflow_with_policy(
            turn_text=turn_text,
            candidate_workflows=candidate_entries,
        )
        ranked_candidate_ids = tuple(
            str(item)
            for item in policy_recommendation.get("ranked_candidate_ids", ())
            if isinstance(item, str) and item
        )
        if ranked_candidate_ids:
            entry_lookup = {
                item["concept_id"]: item for item in candidate_entries if item.get("concept_id")
            }
            reordered_entries: list[dict[str, str]] = []
            for workflow_id in ranked_candidate_ids:
                entry = entry_lookup.pop(workflow_id, None)
                if entry is not None:
                    reordered_entries.append(entry)
            reordered_entries.extend(entry_lookup.values())
            candidate_entries = reordered_entries

        policy_score_lookup = {
            str(item.get("workflow_id")): item
            for item in policy_recommendation.get("candidate_scores", ())
            if isinstance(item, Mapping) and isinstance(item.get("workflow_id"), str)
        }
        candidate_lines: list[str] = []
        for entry_row in candidate_entries:
            cid = entry_row["concept_id"]
            candidate_ids.append(cid)
            entry = f"- {cid}: {entry_row['name']}"
            if entry_row["description"]:
                entry += f" — {entry_row['description']}"
            policy_score = policy_score_lookup.get(cid)
            if policy_score is not None and policy_recommendation.get("policy_active"):
                entry += (
                    " "
                    f"[policy prior {float(policy_score.get('average_reward', 0.0)):.2f}; "
                    f"exploration {float(policy_score.get('exploration_bonus', 0.0)):.2f}; "
                    f"evidence {int(policy_score.get('attempts', 0))}]"
                )
            candidate_lines.append(entry)

        # If no candidates were provided (e.g. empty capability index),
        # inject the default workflow as the sole candidate.
        if not candidate_ids:
            candidate_ids.append(self._default_workflow_id)
            candidate_entries.append(
                {
                    "concept_id": self._default_workflow_id,
                    "name": "Default workflow",
                    "description": "",
                }
            )
            candidate_lines.append(
                f"- {self._default_workflow_id}: Default workflow"
            )

        candidate_list = "\n".join(candidate_lines)
        immutable_candidate_entries = tuple(
            {
                "concept_id": str(entry.get("concept_id") or ""),
                "name": str(entry.get("name") or ""),
                "description": str(entry.get("description") or ""),
            }
            for entry in candidate_entries
        )

        try:
            prompt = self._prompt_service.render_prompt(
                self._classifier_prompt_ids,
                variables={"turn_text": turn_text, "candidate_list": candidate_list},
                fallback=None,
                max_chars=6000,
            )
        except Exception as exc:
            return WorkflowSelectionPrompt(
                prompt_id=None,
                prompt_text=None,
                discovered_workflow_ids=tuple(candidate_ids),
                candidate_entries=immutable_candidate_entries,
                policy_recommendation=policy_recommendation,
                prompt_failure_reason=SELECTOR_PROMPT_RENDER_ERROR_REASON,
                prompt_failure_detail=str(exc),
            )

        if prompt is None or not isinstance(prompt.text, str) or not prompt.text.strip():
            return WorkflowSelectionPrompt(
                prompt_id=None,
                prompt_text=None,
                discovered_workflow_ids=tuple(candidate_ids),
                candidate_entries=immutable_candidate_entries,
                policy_recommendation=policy_recommendation,
                prompt_failure_reason=SELECTOR_PROMPT_UNAVAILABLE_REASON,
            )

        prompt_text = prompt.text.strip()
        if candidate_list and candidate_list not in prompt_text:
            return WorkflowSelectionPrompt(
                prompt_id=prompt.prompt_id,
                prompt_text=prompt_text,
                discovered_workflow_ids=tuple(candidate_ids),
                candidate_entries=immutable_candidate_entries,
                policy_recommendation=policy_recommendation,
                prompt_failure_reason=SELECTOR_PROMPT_MISSING_CANDIDATE_LIST_REASON,
            )
        if turn_text and turn_text not in prompt_text:
            return WorkflowSelectionPrompt(
                prompt_id=prompt.prompt_id,
                prompt_text=prompt_text,
                discovered_workflow_ids=tuple(candidate_ids),
                candidate_entries=immutable_candidate_entries,
                policy_recommendation=policy_recommendation,
                prompt_failure_reason=SELECTOR_PROMPT_MISSING_TURN_TEXT_REASON,
            )

        return WorkflowSelectionPrompt(
            prompt_id=prompt.prompt_id,
            prompt_text=prompt_text,
            discovered_workflow_ids=tuple(candidate_ids),
            candidate_entries=immutable_candidate_entries,
            policy_recommendation=policy_recommendation,
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

        *discovered_workflow_ids* contains all eligible workflow IDs for the
        turn. The selector resolves the model output against that set.
        """
        return self._resolve_rag_first(
            raw_response=raw_response,
            prompt_id=prompt_id,
            prompt_used=prompt_used,
            candidate_workflow_ids=discovered_workflow_ids,
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
            selection_source="selector",
        )

    def resolve_policy_selection(
        self,
        *,
        workflow_id: str,
        prompt_id: Optional[str],
        prompt_used: str | None,
        discovered_workflow_ids: Sequence[str] = (),
        confidence_score: float = 0.0,
        reasoning: str = "",
        selection_metadata: Mapping[str, Any] | None = None,
    ) -> WorkflowSelection:
        """Resolve a direct learned-policy recommendation into a selection."""

        candidate_ids = tuple(
            item for item in discovered_workflow_ids if isinstance(item, str) and item
        )
        clean_workflow_id = str(workflow_id or "").strip()
        if clean_workflow_id not in candidate_ids:
            clean_workflow_id = self._default_workflow_id
            verdict = "rag_default"
        else:
            verdict = "policy_selected"

        return WorkflowSelection(
            workflow_id=clean_workflow_id,
            verdict=verdict,
            prompt_id=prompt_id,
            prompt_used=prompt_used,
            raw_response=f"policy::{clean_workflow_id}",
            discovered_workflow_ids=candidate_ids,
            confidence_score=max(0.0, min(1.0, float(confidence_score))),
            reasoning=str(reasoning or ""),
            selection_source="policy_direct",
            selection_metadata=dict(selection_metadata or {}),
        )

    def resolve_prompt_unavailable_selection(
        self,
        *,
        selection_prompt: WorkflowSelectionPrompt,
    ) -> WorkflowSelection:
        """Return an explicit fail-closed selection when prompt authority is missing."""

        failure_reason = (
            selection_prompt.prompt_failure_reason or SELECTOR_PROMPT_UNAVAILABLE_REASON
        )
        selection_metadata: dict[str, Any] = {
            "prompt_failure_reason": failure_reason,
        }
        if (
            isinstance(selection_prompt.prompt_failure_detail, str)
            and selection_prompt.prompt_failure_detail
        ):
            selection_metadata["prompt_failure_detail"] = (
                selection_prompt.prompt_failure_detail
            )
        return WorkflowSelection(
            workflow_id=self._default_workflow_id,
            verdict=failure_reason,
            prompt_id=selection_prompt.prompt_id,
            prompt_used=selection_prompt.prompt_text,
            raw_response="",
            discovered_workflow_ids=selection_prompt.discovered_workflow_ids,
            confidence_score=0.0,
            reasoning=failure_reason,
            selection_source=SELECTOR_FAIL_CLOSED_SOURCE,
            selection_metadata=selection_metadata,
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
            discovered_match = discovered_lookup.get(normalised)
            if discovered_match:
                return discovered_match

        lowered = raw_text.lower()
        for workflow_id in discovered_ids:
            if workflow_id.lower() in lowered:
                return workflow_id

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
    def _normalise_candidate(cls, value: str) -> str:
        return str(value or "").strip().strip(cls._CANDIDATE_BOUNDARY_STRIP).lower()
