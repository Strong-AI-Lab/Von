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
import re
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
SELECTOR_PROMPT_MAX_CHARS = 24000
SELECTOR_CANDIDATE_DESCRIPTION_MAX_CHARS = 900
SELECTOR_POLICY_REASONING_MAX_CHARS = 280
SELECTOR_POLICY_FRAGMENT_MAX_CANDIDATES = 3


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
    candidate_list_text: str | None = None
    continuation_routing_context_text: str | None = None
    requested_prompt_ids: tuple[str, ...] = ()
    prompt_provenance: Mapping[str, Any] = field(default_factory=dict)
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
    _JSON_WORKFLOW_INPUT_KEYS = ("workflow_inputs", "inputs", "launch_inputs")
    _CANDIDATE_BOUNDARY_STRIP = " \t\r\n`'\".,;:!?()[]{}<>"
    _WORKFLOW_CONCEPT_ID_PATTERN = re.compile(r"#v#[a-z0-9_]+", flags=re.IGNORECASE)
    _REASONING_SELECTION_CUES = (
        "best fit is",
        "best match is",
        "best workflow is",
        "choose",
        "choose the",
        "select",
        "selected",
        "prefer",
        "prefer the",
        "route to",
        "route through",
        "fallback to",
        "fall back to",
    )

    @staticmethod
    def _humanise_candidate_signal(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return " ".join(text.replace("_", " ").replace("-", " ").split())

    @staticmethod
    def _format_candidate_percentage(value: Any) -> str | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number < 0:
            return None
        if number <= 1.0:
            return f"{number * 100:.0f}%"
        return f"{number:.2f}"

    @staticmethod
    def _compact_candidate_description(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = " ".join(text.split())
        if len(text) <= SELECTOR_CANDIDATE_DESCRIPTION_MAX_CHARS:
            return text
        return (
            text[:SELECTOR_CANDIDATE_DESCRIPTION_MAX_CHARS].rstrip()
            + f"... [truncated {len(text) - SELECTOR_CANDIDATE_DESCRIPTION_MAX_CHARS} chars]"
        )

    @classmethod
    def _build_candidate_evidence_items(
        cls,
        entry_row: Mapping[str, Any],
        *,
        policy_score: Mapping[str, Any] | None,
        policy_active: bool,
    ) -> list[str]:
        evidence_items: list[str] = []

        match_source = cls._humanise_candidate_signal(entry_row.get("match_source"))
        if match_source:
            evidence_items.append(f"source {match_source}")

        candidate_source = cls._humanise_candidate_signal(
            entry_row.get("candidate_source")
        )
        if candidate_source:
            evidence_items.append(f"candidate source {candidate_source}")

        relevance = cls._format_candidate_percentage(entry_row.get("relevance_score"))
        if relevance:
            evidence_items.append(f"relevance {relevance}")

        confidence = cls._format_candidate_percentage(entry_row.get("confidence_score"))
        if confidence:
            evidence_items.append(f"confidence {confidence}")

        routing_role = cls._humanise_candidate_signal(
            entry_row.get("routing_profile_role")
        )
        if routing_role:
            evidence_items.append(f"routing role {routing_role}")

        if entry_row.get("routing_eligible") is True:
            evidence_items.append("routing eligible")
        elif entry_row.get("routing_eligible") is False:
            evidence_items.append("routing ineligible")

        if entry_row.get("is_executable") is True:
            evidence_items.append("executable")
        elif entry_row.get("is_executable") is False:
            evidence_items.append("not executable")

        executability_reason = cls._humanise_candidate_signal(
            entry_row.get("executability_reason")
        )
        if executability_reason and executability_reason != "executable now":
            evidence_items.append(f"executability {executability_reason}")

        routing_exclusion_reason = cls._humanise_candidate_signal(
            entry_row.get("routing_exclusion_reason")
        )
        if routing_exclusion_reason:
            evidence_items.append(f"routing exclusion {routing_exclusion_reason}")

        if entry_row.get("is_policy_safe") is True:
            evidence_items.append("policy safe")
        elif entry_row.get("is_policy_safe") is False:
            evidence_items.append("policy unsafe")

        policy_flags = entry_row.get("routing_policy_flags")
        if isinstance(policy_flags, Mapping):
            if bool(policy_flags.get("authoring_intent_required")):
                evidence_items.append("requires authoring intent")
            if bool(policy_flags.get("explicit_workflow_context_required")):
                evidence_items.append("requires workflow context")
            if bool(policy_flags.get("prefer_existing_capability")):
                evidence_items.append("prefer existing capability")

        if policy_score is not None and policy_active:
            evidence_items.extend(
                [
                    f"policy prior {float(policy_score.get('average_reward', 0.0)):.2f}",
                    f"exploration {float(policy_score.get('exploration_bonus', 0.0)):.2f}",
                    f"evidence {int(policy_score.get('attempts', 0))}",
                ]
            )

        return evidence_items

    @staticmethod
    def _candidate_entry_lookup(
        candidate_entries: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Mapping[str, Any]]:
        lookup: dict[str, Mapping[str, Any]] = {}
        for entry in candidate_entries or ():
            if not isinstance(entry, Mapping):
                continue
            concept_id = str(entry.get("concept_id") or "").strip()
            if concept_id:
                lookup[concept_id] = entry
        return lookup

    @staticmethod
    def _is_selector_default_candidate(entry: Mapping[str, Any]) -> bool:
        candidate_source = str(entry.get("candidate_source") or "").strip()
        candidate_reason = str(entry.get("candidate_reason") or "").strip()
        return candidate_source == "selector_default" or candidate_reason in {
            "builtin_selector_candidate",
            "default_workflow_fallback",
        }

    def _resolve_single_specialised_candidate_recovery(
        self,
        *,
        candidate_workflow_ids: Sequence[str],
        candidate_entries: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, str] | None:
        """Recover the only eligible specialised candidate from selector fallback.

        This recovery is intentionally narrow: it only applies when the selector
        has already fallen back because its output was invalid or unmatched, and
        the candidate surface already contains exactly one non-default,
        routing-eligible, executable, policy-safe specialised workflow.
        """

        eligible_specialised_candidates = (
            self._eligible_specialised_candidate_summaries(
                candidate_workflow_ids=candidate_workflow_ids,
                candidate_entries=candidate_entries,
                require_explicit_candidate_flags=True,
            )
        )
        if len(eligible_specialised_candidates) != 1:
            return None
        return next(iter(eligible_specialised_candidates.values()))

    def _eligible_specialised_candidate_summaries(
        self,
        *,
        candidate_workflow_ids: Sequence[str],
        candidate_entries: Sequence[Mapping[str, Any]] | None,
        require_explicit_candidate_flags: bool = False,
    ) -> dict[str, dict[str, str]]:
        """Return non-default candidates that are already eligible for routing."""

        candidate_lookup = {
            str(workflow_id).strip().lower(): str(workflow_id).strip()
            for workflow_id in candidate_workflow_ids
            if isinstance(workflow_id, str) and str(workflow_id).strip()
        }
        if not candidate_lookup:
            return {}

        eligible_specialised_candidates: dict[str, dict[str, str]] = {}
        for entry in candidate_entries or ():
            if not isinstance(entry, Mapping):
                continue
            concept_id = str(entry.get("concept_id") or "").strip()
            if not concept_id:
                continue
            matched_workflow_id = candidate_lookup.get(concept_id.lower())
            if not matched_workflow_id:
                continue
            if matched_workflow_id.lower() == self._default_workflow_id.lower():
                continue
            if self._is_selector_default_candidate(entry):
                continue
            if require_explicit_candidate_flags and not (
                entry.get("routing_eligible") is True
                and entry.get("is_executable") is True
                and entry.get("is_policy_safe") is True
            ):
                continue
            if entry.get("routing_eligible") is False:
                continue
            if entry.get("is_executable") is False:
                continue
            if entry.get("is_policy_safe") is False:
                continue
            if entry.get("turn_launchable") is False:
                continue
            eligible_specialised_candidates[matched_workflow_id.lower()] = {
                "workflow_id": matched_workflow_id,
                "name": str(entry.get("name") or "").strip() or matched_workflow_id,
                "candidate_source": str(entry.get("candidate_source") or "").strip(),
            }

        return eligible_specialised_candidates

    def _generic_default_candidate_review_payload(
        self,
        *,
        selected_workflow_id: str,
        candidate_workflow_ids: Sequence[str],
        candidate_entries: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Describe eligible discovered candidates left behind by a generic route."""

        entry_lookup = self._candidate_entry_lookup(candidate_entries)
        selected_entry = entry_lookup.get(selected_workflow_id)
        if not isinstance(selected_entry, Mapping):
            return None
        if not self._is_selector_default_candidate(selected_entry):
            return None

        eligible_specialised_candidates = (
            self._eligible_specialised_candidate_summaries(
                candidate_workflow_ids=candidate_workflow_ids,
                candidate_entries=candidate_entries,
            )
        )
        if not eligible_specialised_candidates:
            return None

        candidate_ids = [
            item["workflow_id"] for item in eligible_specialised_candidates.values()
        ]
        return {
            "generic_builtin_selection_has_eligible_discovered_candidates": True,
            "eligible_specialised_candidate_count": len(candidate_ids),
            "eligible_specialised_candidate_ids": candidate_ids,
        }

    @classmethod
    def _derive_disqualifying_reason_for_generic_selection(
        cls,
        *,
        selected_workflow_id: str,
        candidate_entries: Sequence[Mapping[str, Any]] | None,
    ) -> str:
        entry_lookup = cls._candidate_entry_lookup(candidate_entries)
        selected_entry = entry_lookup.get(selected_workflow_id)
        if not isinstance(selected_entry, Mapping):
            return ""

        selected_source = str(selected_entry.get("candidate_source") or "").strip()
        selected_reason = str(selected_entry.get("candidate_reason") or "").strip()
        if selected_source != "selector_default" and selected_reason != "builtin_selector_candidate":
            return ""

        disqualified_candidates: list[str] = []
        for concept_id, entry in entry_lookup.items():
            if concept_id == selected_workflow_id:
                continue
            candidate_source = str(entry.get("candidate_source") or "").strip()
            if candidate_source != "workflow_discovery":
                continue

            routing_exclusion_reason = cls._humanise_candidate_signal(
                entry.get("routing_exclusion_reason")
            )
            executability_reason = cls._humanise_candidate_signal(
                entry.get("executability_reason")
            )

            if entry.get("routing_eligible") is False:
                if routing_exclusion_reason:
                    disqualified_candidates.append(
                        f"{concept_id} routing exclusion {routing_exclusion_reason}"
                    )
                else:
                    disqualified_candidates.append(
                        f"{concept_id} is routing ineligible"
                    )
                continue
            if entry.get("is_executable") is False:
                if executability_reason:
                    disqualified_candidates.append(
                        f"{concept_id} executability {executability_reason}"
                    )
                else:
                    disqualified_candidates.append(
                        f"{concept_id} is not executable"
                    )
                continue

            if routing_exclusion_reason:
                disqualified_candidates.append(
                    f"{concept_id} routing exclusion {routing_exclusion_reason}"
                )
                continue

            if executability_reason and executability_reason != "executable now":
                disqualified_candidates.append(
                    f"{concept_id} executability {executability_reason}"
                )

        if not disqualified_candidates:
            return ""
        return (
            "Generic fallback selected because "
            + "; ".join(disqualified_candidates[:2])
        )

    @staticmethod
    def _normalise_reasoning_surface(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = text.replace("#v#", "")
        text = re.sub(r"[_\-]+", " ", text)
        text = re.sub(r"[^a-z0-9\s]+", " ", text)
        return " ".join(text.split())

    @classmethod
    def _candidate_reasoning_aliases(
        cls,
        *,
        workflow_id: str,
        entry: Mapping[str, Any] | None,
    ) -> tuple[str, ...]:
        aliases: set[str] = set()

        def _add(value: Any) -> None:
            normalised = cls._normalise_reasoning_surface(value)
            if len(normalised) < 4:
                return
            aliases.add(normalised)
            if normalised.endswith(" workflow"):
                trimmed = normalised[: -len(" workflow")].strip()
                if len(trimmed) >= 4:
                    aliases.add(trimmed)

        _add(workflow_id)
        if isinstance(entry, Mapping):
            _add(entry.get("name"))

        return tuple(sorted(aliases, key=len, reverse=True))

    @classmethod
    def _reasoning_recommends_alias(cls, *, reasoning: str, alias: str) -> bool:
        if not reasoning or not alias:
            return False
        for cue in cls._REASONING_SELECTION_CUES:
            pattern = (
                rf"\b{re.escape(cue)}\b(?:\s+\w+){{0,6}}\s+"
                rf"{re.escape(alias)}\b"
            )
            if re.search(pattern, reasoning):
                return True
        return False

    @classmethod
    def _resolve_reasoning_recommended_candidate(
        cls,
        *,
        reasoning: str,
        candidate_workflow_ids: Sequence[str],
        candidate_entries: Sequence[Mapping[str, Any]] | None,
    ) -> str | None:
        reasoning_surface = cls._normalise_reasoning_surface(reasoning)
        if not reasoning_surface:
            return None

        entry_lookup = cls._candidate_entry_lookup(candidate_entries)
        matched_candidates: list[str] = []
        for workflow_id in candidate_workflow_ids:
            aliases = cls._candidate_reasoning_aliases(
                workflow_id=workflow_id,
                entry=entry_lookup.get(workflow_id),
            )
            if any(
                cls._reasoning_recommends_alias(
                    reasoning=reasoning_surface,
                    alias=alias,
                )
                for alias in aliases
            ):
                matched_candidates.append(workflow_id)

        if len(matched_candidates) != 1:
            return None
        return matched_candidates[0]

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

    @staticmethod
    def _normalise_policy_recommendation(
        policy_recommendation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        recommendation = (
            dict(policy_recommendation)
            if isinstance(policy_recommendation, Mapping)
            else {}
        )
        guidance_mode = str(recommendation.get("guidance_mode") or "").strip().lower()
        if guidance_mode == "direct":
            recommendation["guidance_mode"] = "prompt_guidance"
            recommendation["hard_direct_guidance_removed"] = True
        return recommendation

    @staticmethod
    def _format_policy_fragment_number(value: Any) -> str | None:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _truncate_policy_reasoning(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) <= SELECTOR_POLICY_REASONING_MAX_CHARS:
            return text
        return (
            text[:SELECTOR_POLICY_REASONING_MAX_CHARS].rstrip()
            + f"... [truncated {len(text) - SELECTOR_POLICY_REASONING_MAX_CHARS} chars]"
        )

    @classmethod
    def _build_routing_policy_fragments(cls, policy_recommendation: Mapping[str, Any]) -> str:
        if not bool(policy_recommendation.get("policy_active")):
            return "No learned routing policy guidance is active."

        lines = ["Learned routing policy guidance:"]
        recommended_workflow_id = str(
            policy_recommendation.get("recommended_workflow_id") or ""
        ).strip()
        if recommended_workflow_id:
            lines.append(f"- Recommended workflow: {recommended_workflow_id}")

        confidence = cls._format_policy_fragment_number(
            policy_recommendation.get("confidence_score")
        )
        if confidence is not None:
            lines.append(f"- Policy confidence: {confidence}")

        reasoning = cls._truncate_policy_reasoning(
            policy_recommendation.get("reasoning")
        )
        if reasoning:
            lines.append(f"- Policy reasoning: {reasoning}")

        candidate_scores = policy_recommendation.get("candidate_scores")
        if isinstance(candidate_scores, Sequence):
            score_lines: list[str] = []
            for item in candidate_scores[:SELECTOR_POLICY_FRAGMENT_MAX_CANDIDATES]:
                if not isinstance(item, Mapping):
                    continue
                workflow_id = str(item.get("workflow_id") or "").strip()
                if not workflow_id:
                    continue
                fragments: list[str] = []
                rank = item.get("rank")
                if isinstance(rank, int):
                    fragments.append(f"rank {rank}")
                score = cls._format_policy_fragment_number(item.get("score"))
                if score is not None:
                    fragments.append(f"score {score}")
                attempts = item.get("attempts")
                if isinstance(attempts, (int, float)):
                    fragments.append(f"evidence {int(attempts)}")
                candidate_reasoning = cls._truncate_policy_reasoning(item.get("reasoning"))
                if candidate_reasoning:
                    fragments.append(candidate_reasoning)
                if fragments:
                    score_lines.append(f"- {workflow_id}: {'; '.join(fragments)}")
                else:
                    score_lines.append(f"- {workflow_id}")

            if score_lines:
                lines.append("Top policy candidates:")
                lines.extend(score_lines)

        return "\n".join(lines)

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
            candidate_entries=selection_prompt.candidate_entries,
        )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def prepare_selection_prompt(
        self,
        *,
        turn_text: str,
        discovered_workflows: Sequence[Mapping[str, Any]] | None = None,
        continuation_routing_context_text: str | None = None,
    ) -> WorkflowSelectionPrompt:
        """Build the selection prompt.

        *discovered_workflows* contains all currently eligible candidates
        (including built-in workflows). The prompt lists every candidate so
        the LLM ranker can choose the best one.
        """
        return self._prepare_rag_first_prompt(
            turn_text=turn_text,
            candidate_workflows=discovered_workflows,
            continuation_routing_context_text=continuation_routing_context_text,
        )

    # ------------------------------------------------------------------
    # RAG-first prompt (Phase 2)
    # ------------------------------------------------------------------

    def _prepare_rag_first_prompt(
        self,
        *,
        turn_text: str,
        candidate_workflows: Sequence[Mapping[str, Any]] | None = None,
        continuation_routing_context_text: str | None = None,
    ) -> WorkflowSelectionPrompt:
        """Build the RAG-first ranker prompt from candidate workflows."""
        candidate_ids: list[str] = []
        candidate_entries: list[dict[str, Any]] = []
        requested_prompt_ids = tuple(
            concept_id
            for concept_id in self._classifier_prompt_ids
            if isinstance(concept_id, str) and concept_id.strip()
        )

        if candidate_workflows:
            for wf in candidate_workflows:
                cid = str(wf.get("concept_id") or "").strip()
                if not cid:
                    continue
                entry: dict[str, Any] = {
                    "concept_id": cid,
                    "name": str(wf.get("name") or cid),
                    "description": self._compact_candidate_description(
                        wf.get("description")
                    ),
                }
                for field_name in (
                    "match_source",
                    "executability_reason",
                    "executability_detail",
                    "routing_exclusion_reason",
                    "routing_profile_role",
                    "routing_profile_role_source",
                    "candidate_source",
                    "candidate_reason",
                ):
                    value = wf.get(field_name)
                    if isinstance(value, str) and value.strip():
                        entry[field_name] = value.strip()
                for field_name in (
                    "relevance_score",
                    "confidence_score",
                    "policy_average_reward",
                    "policy_exploration_bonus",
                    "policy_attempts",
                    "policy_rank",
                ):
                    value = wf.get(field_name)
                    if value is None:
                        continue
                    entry[field_name] = value
                for field_name in (
                    "is_executable",
                    "is_policy_safe",
                    "routing_eligible",
                ):
                    value = wf.get(field_name)
                    if isinstance(value, bool):
                        entry[field_name] = value
                for field_name in (
                    "routing_profile",
                    "routing_policy_flags",
                    "routing_policy_lexical_signals",
                ):
                    value = wf.get(field_name)
                    if isinstance(value, Mapping):
                        entry[field_name] = {
                            str(key): nested_value
                            for key, nested_value in value.items()
                            if isinstance(key, str)
                        }
                candidate_entries.append(entry)

        policy_recommendation = self._normalise_policy_recommendation(
            recommend_workflow_with_policy(
                turn_text=turn_text,
                candidate_workflows=candidate_entries,
            )
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
            reordered_entries: list[dict[str, Any]] = []
            for workflow_id in ranked_candidate_ids:
                candidate_entry = entry_lookup.pop(workflow_id, None)
                if candidate_entry is not None:
                    reordered_entries.append(candidate_entry)
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
            candidate_line = f"- {cid}: {entry_row['name']}"
            if entry_row["description"]:
                candidate_line += f" — {entry_row['description']}"
            policy_score = policy_score_lookup.get(cid)
            evidence_items = self._build_candidate_evidence_items(
                entry_row,
                policy_score=policy_score,
                policy_active=bool(policy_recommendation.get("policy_active")),
            )
            if evidence_items:
                candidate_line += f" [{'; '.join(evidence_items)}]"
            candidate_lines.append(candidate_line)

        # If no candidates were provided (e.g. empty capability index),
        # inject the default workflow as the sole candidate.
        if not candidate_ids:
            candidate_ids.append(self._default_workflow_id)
            candidate_entries.append(
                {
                    "concept_id": self._default_workflow_id,
                    "name": "Default workflow",
                    "description": "",
                    "candidate_source": "selector_default",
                    "candidate_reason": "default_workflow_fallback",
                }
            )
            candidate_lines.append(
                f"- {self._default_workflow_id}: Default workflow"
            )

        candidate_list = "\n".join(candidate_lines)
        continuation_context_text = (
            str(continuation_routing_context_text).strip()
            if isinstance(continuation_routing_context_text, str)
            and continuation_routing_context_text.strip()
            else "No active workflow continuation context."
        )
        render_variables = {
            "turn_text": turn_text,
            "candidate_list": candidate_list,
            "continuation_routing_context": continuation_context_text,
            "selector_routing_context": continuation_context_text,
            "routing_policy_fragments": self._build_routing_policy_fragments(
                policy_recommendation
            ),
        }
        immutable_candidate_entries = tuple(
            {
                str(key): value
                for key, value in entry.items()
                if isinstance(key, str)
            }
            for entry in candidate_entries
        )
        prompt_provenance_base = {
            "prompt_mode": "rag_first_candidate_selector",
            "requested_prompt_ids": list(requested_prompt_ids),
            "resolved_prompt_id": None,
            "render_variables": dict(render_variables),
            "truncated": False,
        }

        try:
            prompt = self._prompt_service.render_prompt(
                requested_prompt_ids,
                variables=render_variables,
                fallback=None,
                max_chars=SELECTOR_PROMPT_MAX_CHARS,
            )
        except Exception as exc:
            return WorkflowSelectionPrompt(
                prompt_id=None,
                prompt_text=None,
                discovered_workflow_ids=tuple(candidate_ids),
                candidate_entries=immutable_candidate_entries,
                candidate_list_text=candidate_list,
                continuation_routing_context_text=continuation_context_text,
                requested_prompt_ids=requested_prompt_ids,
                prompt_provenance=prompt_provenance_base,
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
                candidate_list_text=candidate_list,
                continuation_routing_context_text=continuation_context_text,
                requested_prompt_ids=requested_prompt_ids,
                prompt_provenance=prompt_provenance_base,
                policy_recommendation=policy_recommendation,
                prompt_failure_reason=SELECTOR_PROMPT_UNAVAILABLE_REASON,
            )

        prompt_text = prompt.text.strip()
        prompt_provenance = {
            **prompt_provenance_base,
            "resolved_prompt_id": prompt.prompt_id,
            "render_variables": dict(prompt.variables),
            "truncated": bool(prompt.truncated),
        }
        if candidate_list and candidate_list not in prompt_text:
            return WorkflowSelectionPrompt(
                prompt_id=prompt.prompt_id,
                prompt_text=None,
                discovered_workflow_ids=tuple(candidate_ids),
                candidate_entries=immutable_candidate_entries,
                candidate_list_text=candidate_list,
                continuation_routing_context_text=continuation_context_text,
                requested_prompt_ids=requested_prompt_ids,
                prompt_provenance=prompt_provenance,
                policy_recommendation=policy_recommendation,
                prompt_failure_reason=SELECTOR_PROMPT_MISSING_CANDIDATE_LIST_REASON,
            )
        if turn_text and turn_text not in prompt_text:
            return WorkflowSelectionPrompt(
                prompt_id=prompt.prompt_id,
                prompt_text=None,
                discovered_workflow_ids=tuple(candidate_ids),
                candidate_entries=immutable_candidate_entries,
                candidate_list_text=candidate_list,
                continuation_routing_context_text=continuation_context_text,
                requested_prompt_ids=requested_prompt_ids,
                prompt_provenance=prompt_provenance,
                policy_recommendation=policy_recommendation,
                prompt_failure_reason=SELECTOR_PROMPT_MISSING_TURN_TEXT_REASON,
            )

        return WorkflowSelectionPrompt(
            prompt_id=prompt.prompt_id,
            prompt_text=prompt_text,
            discovered_workflow_ids=tuple(candidate_ids),
            candidate_entries=immutable_candidate_entries,
            candidate_list_text=candidate_list,
            continuation_routing_context_text=continuation_context_text,
            requested_prompt_ids=requested_prompt_ids,
            prompt_provenance=prompt_provenance,
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
        candidate_entries: Sequence[Mapping[str, Any]] | None = None,
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
            candidate_entries=candidate_entries,
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
        candidate_entries: Sequence[Mapping[str, Any]] | None = None,
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
        selection_metadata: dict[str, Any] = {
            "raw_candidate_label": label,
            "raw_response_format": structured.get("raw_response_format"),
            "structured_selection_detected": structured.get(
                "structured_selection_detected", False
            ),
        }
        workflow_inputs = structured.get("workflow_inputs")
        if isinstance(workflow_inputs, Mapping):
            selection_metadata["workflow_inputs"] = dict(workflow_inputs)
        requested_candidate_workflow_id = self._resolve_requested_candidate_workflow_id(
            raw_response=raw_response,
            raw_candidate_label=label,
            candidate_entries=candidate_entries,
        )
        if requested_candidate_workflow_id:
            selection_metadata["requested_candidate_workflow_id"] = (
                requested_candidate_workflow_id
            )

        # Match against candidate workflow IDs.
        label_lower = label.lower()
        matched_id = candidate_lookup.get(label_lower)

        if matched_id:
            workflow_id = matched_id
            verdict = "rag_selected"
            selection_metadata["selection_resolution"] = "candidate_label_exact_match"
        else:
            # Fallback: try to find a candidate in the raw text.
            raw_text = str(raw_response or "").lower()
            for cid in candidate_ids:
                if cid.lower() in raw_text:
                    workflow_id = cid
                    verdict = "rag_selected"
                    selection_metadata["selection_resolution"] = (
                        "raw_response_contains_candidate_id"
                    )
                    break
            else:
                # Ultimate fallback to default workflow.
                workflow_id = self._default_workflow_id
                verdict = "rag_default"
                if (
                    requested_candidate_workflow_id
                    and requested_candidate_workflow_id.lower()
                    != self._default_workflow_id.lower()
                ):
                    selection_metadata["selection_resolution"] = (
                        "default_workflow_fallback_unmatched_candidate"
                    )
                    selection_metadata["unmatched_candidate_workflow_id"] = (
                        requested_candidate_workflow_id
                    )
                else:
                    selection_metadata["selection_resolution"] = (
                        "default_workflow_fallback"
                    )
                # Lower confidence for fallback selections.
                if confidence_score > 0.0:
                    confidence_score = min(confidence_score, 0.3)
        reasoning_candidate = self._resolve_reasoning_recommended_candidate(
            reasoning=reasoning,
            candidate_workflow_ids=candidate_ids,
            candidate_entries=candidate_entries,
        )
        if reasoning_candidate and reasoning_candidate != workflow_id:
            selection_metadata["selection_resolution"] = "reasoning_candidate_override"
            selection_metadata["reasoning_override_from_workflow_id"] = workflow_id
            selection_metadata["reasoning_override_workflow_id"] = reasoning_candidate
            workflow_id = reasoning_candidate
            verdict = "rag_selected"
        generic_candidate_review = self._generic_default_candidate_review_payload(
            selected_workflow_id=workflow_id,
            candidate_workflow_ids=candidate_ids,
            candidate_entries=candidate_entries,
        )
        if generic_candidate_review:
            selection_metadata.update(generic_candidate_review)
        recovered_candidate = None
        if verdict == "rag_default":
            recovered_candidate = self._resolve_single_specialised_candidate_recovery(
                candidate_workflow_ids=candidate_ids,
                candidate_entries=candidate_entries,
            )
        if recovered_candidate is not None:
            prior_selection_resolution = (
                str(selection_metadata.get("selection_resolution") or "").strip() or None
            )
            prior_workflow_id = workflow_id
            workflow_id = recovered_candidate["workflow_id"]
            verdict = "rag_selected"
            confidence_score = min(confidence_score, 0.3)
            selection_metadata["selection_resolution"] = (
                "single_specialised_candidate_recovery_from_selector_fallback"
            )
            if prior_selection_resolution:
                selection_metadata["selection_resolution_prior"] = (
                    prior_selection_resolution
                )
            selection_metadata["selector_contract_recovery_applied"] = True
            selection_metadata["recovered_from_workflow_id"] = prior_workflow_id
            selection_metadata["recovered_candidate_workflow_id"] = workflow_id
            selection_metadata["recovered_candidate_name"] = (
                recovered_candidate["name"]
            )
            if recovered_candidate.get("candidate_source"):
                selection_metadata["recovered_candidate_source"] = (
                    recovered_candidate["candidate_source"]
                )
            selection_metadata["eligible_specialised_candidate_ids"] = [workflow_id]
            reasoning = (
                "Selector returned off-contract or unmatched output, so the only "
                "eligible specialised candidate already present in the selector "
                "candidate set was selected instead."
            )
        selection_metadata["selected_workflow_id"] = workflow_id

        if not reasoning:
            derived_reason = self._derive_disqualifying_reason_for_generic_selection(
                selected_workflow_id=workflow_id,
                candidate_entries=candidate_entries,
            )
            if derived_reason:
                reasoning = derived_reason
                selection_metadata["derived_reasoning"] = (
                    "generic_fallback_disqualification"
                )

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
            selection_metadata=selection_metadata,
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
            "selection_resolution": "selector_prompt_unavailable_fail_closed",
        }
        if (
            isinstance(selection_prompt.prompt_failure_detail, str)
            and selection_prompt.prompt_failure_detail
        ):
            selection_metadata["prompt_failure_detail"] = (
                selection_prompt.prompt_failure_detail
            )
        recovered_candidate = self._resolve_single_specialised_candidate_recovery(
            candidate_workflow_ids=selection_prompt.discovered_workflow_ids,
            candidate_entries=selection_prompt.candidate_entries,
        )
        if recovered_candidate is not None:
            workflow_id = recovered_candidate["workflow_id"]
            selection_metadata.update(
                {
                    "selection_resolution": (
                        "single_specialised_candidate_recovery_from_selector_prompt_unavailable"
                    ),
                    "selector_contract_recovery_applied": True,
                    "recovered_from_workflow_id": self._default_workflow_id,
                    "recovered_candidate_workflow_id": workflow_id,
                    "recovered_candidate_name": recovered_candidate["name"],
                    "eligible_specialised_candidate_ids": [workflow_id],
                    "selected_workflow_id": workflow_id,
                }
            )
            if recovered_candidate.get("candidate_source"):
                selection_metadata["recovered_candidate_source"] = (
                    recovered_candidate["candidate_source"]
                )
            return WorkflowSelection(
                workflow_id=workflow_id,
                verdict=failure_reason,
                prompt_id=selection_prompt.prompt_id,
                prompt_used=selection_prompt.prompt_text,
                raw_response="",
                discovered_workflow_ids=selection_prompt.discovered_workflow_ids,
                confidence_score=0.0,
                reasoning=(
                    f"{failure_reason}; the only eligible specialised candidate "
                    "already present in the selector candidate set was selected."
                ),
                selection_source=SELECTOR_FAIL_CLOSED_SOURCE,
                selection_metadata=selection_metadata,
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
            return {
                "confidence": 0.0,
                "reasoning": "",
                "structured_selection_detected": False,
                "raw_response_format": "text",
            }

        if not isinstance(parsed, Mapping):
            return {
                "confidence": 0.0,
                "reasoning": "",
                "structured_selection_detected": False,
                "raw_response_format": type(parsed).__name__.lower(),
            }

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

        workflow_inputs: dict[str, Any] = {}
        for key in cls._JSON_WORKFLOW_INPUT_KEYS:
            raw_inputs = parsed.get(key)
            if isinstance(raw_inputs, Mapping):
                workflow_inputs = {
                    str(input_key): input_value
                    for input_key, input_value in raw_inputs.items()
                    if isinstance(input_key, str) and input_key.strip()
                }
                break

        return {
            "confidence": confidence,
            "reasoning": reasoning,
            "workflow_inputs": workflow_inputs,
            "structured_selection_detected": True,
            "raw_response_format": "json_object",
        }

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
    def _extract_any_workflow_concept_id(cls, value: Any) -> str | None:
        raw_text = str(value or "").strip()
        if not raw_text:
            return None
        match = cls._WORKFLOW_CONCEPT_ID_PATTERN.search(raw_text)
        if not match:
            return None
        concept_id = match.group(0).strip()
        if not concept_id:
            return None
        if concept_id.lower().startswith("#v#"):
            return "#V#" + concept_id[3:]
        return concept_id

    @classmethod
    def _resolve_requested_candidate_workflow_id(
        cls,
        *,
        raw_response: Any,
        raw_candidate_label: str,
        candidate_entries: Sequence[Mapping[str, Any]] | None,
    ) -> str | None:
        entry_lookup = cls._candidate_entry_lookup(candidate_entries)
        entry_id_lookup = {
            concept_id.lower(): concept_id for concept_id in entry_lookup.keys()
        }

        structured_candidate = cls._extract_json_candidate(str(raw_response or ""))
        if structured_candidate:
            concept_id = cls._extract_any_workflow_concept_id(structured_candidate)
            if concept_id:
                matched_workflow_id = entry_id_lookup.get(concept_id.lower())
                if matched_workflow_id:
                    return matched_workflow_id
                return concept_id

        for surface in (
            raw_candidate_label,
            str(raw_response or ""),
        ):
            concept_id = cls._extract_any_workflow_concept_id(surface)
            if concept_id:
                matched_workflow_id = entry_id_lookup.get(concept_id.lower())
                if matched_workflow_id:
                    return matched_workflow_id

        normalised_label = cls._normalise_reasoning_surface(raw_candidate_label)
        if not normalised_label:
            return None

        for workflow_id, entry in entry_lookup.items():
            aliases = cls._candidate_reasoning_aliases(
                workflow_id=workflow_id,
                entry=entry,
            )
            if normalised_label in aliases:
                return workflow_id
        return None

    @classmethod
    def _normalise_candidate(cls, value: str) -> str:
        return str(value or "").strip().strip(cls._CANDIDATE_BOUNDARY_STRIP).lower()
