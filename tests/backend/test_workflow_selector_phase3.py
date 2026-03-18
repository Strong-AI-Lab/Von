"""Tests for Phase 3 model-based selection (JVNAUTOSCI-1424 Phase 3).

Tests cover:
- Structured JSON response parsing (confidence + reasoning extraction).
- Confidence score propagation through WorkflowSelection.
- Fallback behaviour when LLM returns plain text (no confidence/reasoning).
- Selection experience ring-buffer recording and snapshots.
- Baseline quality measurement (Phase 2 → Phase 3 parity).
"""

from __future__ import annotations

import json

import pytest

import src.backend.services.workflow_selection_policy_service as policy_module
from src.backend.services.workflow_selection_experience import (
    SelectionExperienceTuple,
    get_recent_experiences,
    get_selection_experience_snapshot,
    record_selection_experience,
    reset_selection_experience,
)
from src.backend.workflows import WorkflowRegistry
from src.backend.workflows.workflow_selector import (
    WorkflowSelection,
    WorkflowSelector,
)
from workflow_test_support import (
    build_test_conversation_turn_registry,
    build_test_prompt_service,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_registry() -> WorkflowRegistry:
    return build_test_conversation_turn_registry()


def _build_selector(*, default_workflow_id=None) -> WorkflowSelector:
    kwargs: dict = {
        "registry": _build_registry(),
        "prompt_service": build_test_prompt_service(),
    }
    if default_workflow_id is not None:
        kwargs["default_workflow_id"] = default_workflow_id
    return WorkflowSelector(**kwargs)


@pytest.fixture(autouse=True)
def _disable_learned_policy(monkeypatch: pytest.MonkeyPatch):
    policy_module.clear_live_selection_policy()
    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)


_TOOL_WORKFLOW = "#V#tool_calling_workflow"
_CHAT_WORKFLOW = "#V#chat_assistant_workflow"
_NARRATION_WORKFLOW = "#V#chat_narration_workflow"

_CANDIDATES = [_CHAT_WORKFLOW, _TOOL_WORKFLOW, _NARRATION_WORKFLOW]


# ---------------------------------------------------------------------------
# Structured response parsing
# ---------------------------------------------------------------------------


class TestStructuredResponseParsing:
    """Test _parse_structured_selection for JSON confidence + reasoning."""

    def test_parse_full_json_response(self):
        result = WorkflowSelector._parse_structured_selection(
            raw_response=json.dumps({
                "workflow_id": _TOOL_WORKFLOW,
                "confidence": 0.95,
                "reasoning": "The request requires tool access.",
            }),
        )
        assert result["confidence"] == pytest.approx(0.95)
        assert result["reasoning"] == "The request requires tool access."

    def test_parse_confidence_clamped_to_range(self):
        result = WorkflowSelector._parse_structured_selection(
            raw_response=json.dumps({
                "workflow_id": _TOOL_WORKFLOW,
                "confidence": 1.5,
                "reasoning": "Over-confident.",
            }),
        )
        assert result["confidence"] == 1.0  # Clamped to max.

    def test_parse_negative_confidence_clamped(self):
        result = WorkflowSelector._parse_structured_selection(
            raw_response=json.dumps({
                "workflow_id": _TOOL_WORKFLOW,
                "confidence": -0.3,
            }),
        )
        assert result["confidence"] == 0.0  # Clamped to min.

    def test_parse_alternative_confidence_keys(self):
        for key in ("confidence", "confidence_score", "score"):
            result = WorkflowSelector._parse_structured_selection(
                raw_response=json.dumps({
                    "workflow_id": _TOOL_WORKFLOW,
                    key: 0.7,
                }),
            )
            assert result["confidence"] == pytest.approx(0.7), f"Failed for key: {key}"

    def test_parse_alternative_reasoning_keys(self):
        for key in ("reasoning", "reason", "explanation", "rationale"):
            result = WorkflowSelector._parse_structured_selection(
                raw_response=json.dumps({
                    "workflow_id": _TOOL_WORKFLOW,
                    key: "Some reasoning text.",
                }),
            )
            assert result["reasoning"] == "Some reasoning text.", f"Failed for key: {key}"

    def test_parse_plain_text_returns_defaults(self):
        result = WorkflowSelector._parse_structured_selection(
            raw_response=_TOOL_WORKFLOW,
        )
        assert result["confidence"] == 0.0
        assert result["reasoning"] == ""

    def test_parse_empty_response_returns_defaults(self):
        result = WorkflowSelector._parse_structured_selection(
            raw_response="",
        )
        assert result["confidence"] == 0.0
        assert result["reasoning"] == ""

    def test_parse_none_response_returns_defaults(self):
        result = WorkflowSelector._parse_structured_selection(
            raw_response=None,
        )
        assert result["confidence"] == 0.0
        assert result["reasoning"] == ""

    def test_parse_json_without_confidence(self):
        result = WorkflowSelector._parse_structured_selection(
            raw_response=json.dumps({"workflow_id": _TOOL_WORKFLOW}),
        )
        assert result["confidence"] == 0.0
        assert result["reasoning"] == ""

    def test_parse_json_string_value(self):
        """JSON that parses to a plain string, not an object."""
        result = WorkflowSelector._parse_structured_selection(
            raw_response=json.dumps(_TOOL_WORKFLOW),
        )
        assert result["confidence"] == 0.0
        assert result["reasoning"] == ""


# ---------------------------------------------------------------------------
# Confidence + reasoning in WorkflowSelection
# ---------------------------------------------------------------------------


class TestSelectionConfidenceReasoning:
    """Test confidence and reasoning propagation in resolve_selection."""

    def test_structured_json_populates_confidence_and_reasoning(self):
        selector = _build_selector()
        result = selector.resolve_selection(
            raw_response=json.dumps({
                "workflow_id": _TOOL_WORKFLOW,
                "confidence": 0.92,
                "reasoning": "Request needs external data.",
            }),
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=_CANDIDATES,
        )
        assert result.workflow_id == _TOOL_WORKFLOW
        assert result.verdict == "rag_selected"
        assert result.confidence_score == pytest.approx(0.92)
        assert result.reasoning == "Request needs external data."

    def test_plain_text_response_has_zero_confidence(self):
        selector = _build_selector()
        result = selector.resolve_selection(
            raw_response=_TOOL_WORKFLOW,
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=_CANDIDATES,
        )
        assert result.workflow_id == _TOOL_WORKFLOW
        assert result.verdict == "rag_selected"
        assert result.confidence_score == 0.0
        assert result.reasoning == ""

    def test_fallback_caps_confidence(self):
        """When falling back to default, confidence is capped at 0.3."""
        selector = _build_selector(default_workflow_id=_CHAT_WORKFLOW)
        result = selector.resolve_selection(
            raw_response=json.dumps({
                "workflow_id": "#V#nonexistent_workflow",
                "confidence": 0.8,
                "reasoning": "Guessing.",
            }),
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=[_TOOL_WORKFLOW, _NARRATION_WORKFLOW],
        )
        assert result.workflow_id == _CHAT_WORKFLOW
        assert result.verdict == "rag_default"
        assert result.confidence_score <= 0.3

    def test_fallback_with_zero_confidence_stays_zero(self):
        selector = _build_selector(default_workflow_id=_CHAT_WORKFLOW)
        result = selector.resolve_selection(
            raw_response="completely_unknown",
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=[_TOOL_WORKFLOW],
        )
        assert result.verdict == "rag_default"
        assert result.confidence_score == 0.0

    def test_free_form_legacy_label_falls_back_to_default_without_reasoning(self):
        selector = _build_selector(default_workflow_id=_CHAT_WORKFLOW)
        result = selector.resolve_selection(
            raw_response="tool_seeking",
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=[_TOOL_WORKFLOW],
        )
        assert result.workflow_id == _CHAT_WORKFLOW
        assert result.verdict == "rag_default"
        assert result.confidence_score == 0.0
        assert result.reasoning == ""


# ---------------------------------------------------------------------------
# Enhanced prompt format
# ---------------------------------------------------------------------------


class TestEnhancedPrompt:
    """Verify the Phase 3 prompt requests structured JSON output."""

    def test_prompt_requests_json_output(self):
        selector = _build_selector()
        prompt = selector.prepare_selection_prompt(
            turn_text="Find papers about transformers",
            discovered_workflows=[
                {
                    "concept_id": _TOOL_WORKFLOW,
                    "name": "Tool Calling",
                    "description": "General-purpose tool pipeline.",
                },
            ],
        )
        assert prompt.prompt_text is not None
        assert "JSON" in prompt.prompt_text
        assert "confidence" in prompt.prompt_text
        assert "reasoning" in prompt.prompt_text

    def test_prompt_preserves_candidate_list(self):
        selector = _build_selector()
        prompt = selector.prepare_selection_prompt(
            turn_text="hello",
            discovered_workflows=[
                {"concept_id": _CHAT_WORKFLOW, "name": "Chat", "description": "Chat."},
                {"concept_id": _TOOL_WORKFLOW, "name": "Tools", "description": "Tools."},
            ],
        )
        assert prompt.prompt_text is not None
        assert _CHAT_WORKFLOW in prompt.prompt_text
        assert _TOOL_WORKFLOW in prompt.prompt_text


# ---------------------------------------------------------------------------
# Selection experience recording
# ---------------------------------------------------------------------------


class TestSelectionExperience:
    """Test the process-local experience ring buffer."""

    @pytest.fixture(autouse=True)
    def _reset_buffer(self):
        reset_selection_experience()
        yield
        reset_selection_experience()

    def test_record_single_experience(self):
        entry = record_selection_experience(
            turn_id="turn-1",
            query="Find papers",
            candidate_workflow_ids=[_CHAT_WORKFLOW, _TOOL_WORKFLOW],
            selected_workflow_id=_TOOL_WORKFLOW,
            verdict="rag_selected",
            confidence_score=0.9,
            reasoning="Needs tools.",
            model_name="gpt-4",
            routing_duration_ms=42.0,
        )
        assert isinstance(entry, SelectionExperienceTuple)
        assert entry.selected_workflow_id == _TOOL_WORKFLOW
        assert entry.confidence_score == pytest.approx(0.9)
        assert entry.candidate_count == 2

    def test_snapshot_aggregates(self):
        record_selection_experience(
            verdict="rag_selected", confidence_score=0.8,
        )
        record_selection_experience(
            verdict="rag_default", confidence_score=0.2,
        )
        record_selection_experience(
            verdict="rag_selected", confidence_score=0.95,
        )

        snap = get_selection_experience_snapshot()
        agg = snap["aggregates"]
        assert agg["total_selections"] == 3
        assert agg["rag_selected"] == 2
        assert agg["rag_default"] == 1
        assert agg["avg_confidence"] == pytest.approx((0.8 + 0.2 + 0.95) / 3, rel=1e-3)

    def test_recent_experiences_newest_first(self):
        for i in range(5):
            record_selection_experience(
                turn_id=f"turn-{i}",
                verdict="rag_selected",
            )
        recent = get_recent_experiences(limit=3)
        assert len(recent) == 3
        assert recent[0]["turn_id"] == "turn-4"
        assert recent[2]["turn_id"] == "turn-2"

    def test_buffer_bounded_capacity(self):
        for i in range(600):
            record_selection_experience(
                turn_id=f"t-{i}", verdict="rag_selected",
            )
        snap = get_selection_experience_snapshot()
        assert len(snap["recent_experiences"]) == 500  # Default capacity.
        assert snap["aggregates"]["total_selections"] == 600  # Aggregates count all.

    def test_experience_tuple_to_dict(self):
        entry = record_selection_experience(
            turn_id="turn-x",
            query="test query",
            selected_workflow_id=_TOOL_WORKFLOW,
            verdict="rag_selected",
            confidence_score=0.85,
            reasoning="Tool needed.",
        )
        d = entry.to_dict()
        assert d["turn_id"] == "turn-x"
        assert d["confidence_score"] == pytest.approx(0.85)
        assert d["reasoning"] == "Tool needed."
        assert "timestamp" in d

    def test_reset_clears_buffer_and_aggregates(self):
        record_selection_experience(verdict="rag_selected", confidence_score=0.5)
        reset_selection_experience()

        snap = get_selection_experience_snapshot()
        assert snap["aggregates"]["total_selections"] == 0
        assert len(snap["recent_experiences"]) == 0


# ---------------------------------------------------------------------------
# Phase 2 → Phase 3 baseline parity
# ---------------------------------------------------------------------------


class TestBaselineParity:
    """Verify Phase 3 structured output produces equivalent routing.

    These test cases replicate Phase 2 RAG-first scenarios and assert that
    Phase 3 structured JSON responses route to the same workflow, ensuring
    no regression from the prompt format change.
    """

    _PARITY_CASES = [
        # (response, expected_workflow_id, description)
        (
            json.dumps({"workflow_id": _TOOL_WORKFLOW, "confidence": 0.95, "reasoning": "Needs tools."}),
            _TOOL_WORKFLOW,
            "structured JSON tool selection",
        ),
        (
            json.dumps({"workflow_id": _CHAT_WORKFLOW, "confidence": 0.9, "reasoning": "Simple greeting."}),
            _CHAT_WORKFLOW,
            "structured JSON chat selection",
        ),
        (
            _TOOL_WORKFLOW,
            _TOOL_WORKFLOW,
            "plain concept_id (Phase 2 style)",
        ),
        (
            f"I recommend {_TOOL_WORKFLOW} for this task",
            _TOOL_WORKFLOW,
            "verbose response with concept_id embedded",
        ),
        (
            json.dumps({"workflow_id": _NARRATION_WORKFLOW}),
            _NARRATION_WORKFLOW,
            "JSON without confidence (Phase 2 compat)",
        ),
    ]

    @pytest.mark.parametrize(
        "response,expected_id,description",
        _PARITY_CASES,
        ids=[c[2] for c in _PARITY_CASES],
    )
    def test_parity_with_phase2(self, response, expected_id, description):
        selector = _build_selector()
        result = selector.resolve_selection(
            raw_response=response,
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=_CANDIDATES,
        )
        assert result.workflow_id == expected_id, (
            f"Parity failure ({description}): expected {expected_id}, "
            f"got {result.workflow_id}"
        )
        assert result.verdict == "rag_selected"
