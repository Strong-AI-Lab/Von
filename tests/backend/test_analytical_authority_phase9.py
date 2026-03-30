"""Tests for Phase 9 (JVNAUTOSCI-1617): analytical capability is workflow-owned.

Validates:
1. Dead Python analytical-intent regex patterns were removed.
2. Episode evaluation auto-trigger gate emits standalone decision_authority.
3. Prompt requirement inference is explicit-only (no semantic inference).
"""

from __future__ import annotations

import re
from typing import Any


# ---- Dead-code removal verification ------------------------------------

def test_concept_verification_hint_pattern_removed() -> None:
    """The former _PROMPT_CONCEPT_VERIFICATION_HINT_PATTERN is dead code."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    assert not hasattr(
        InternalMCPChatOrchestrator,
        "_PROMPT_CONCEPT_VERIFICATION_HINT_PATTERN",
    )


def test_scholarly_representation_intent_pattern_removed() -> None:
    """The former _PROMPT_SCHOLARLY_REPRESENTATION_INTENT_PATTERN is dead code."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    assert not hasattr(
        InternalMCPChatOrchestrator,
        "_PROMPT_SCHOLARLY_REPRESENTATION_INTENT_PATTERN",
    )


def test_paper_intent_pattern_removed() -> None:
    """The former _PROMPT_PAPER_INTENT_PATTERN is dead code."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    assert not hasattr(
        InternalMCPChatOrchestrator,
        "_PROMPT_PAPER_INTENT_PATTERN",
    )


def test_representation_intent_pattern_removed() -> None:
    """The former _PROMPT_REPRESENTATION_INTENT_PATTERN is dead code."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    assert not hasattr(
        InternalMCPChatOrchestrator,
        "_PROMPT_REPRESENTATION_INTENT_PATTERN",
    )


# ---- Prompt requirement is explicit-only --------------------------------

def test_derive_prompt_tool_requirements_returns_no_scholarly_intent() -> None:
    """scholarly_representation_intent is always False (workflow/LLM-owned)."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    result = InternalMCPChatOrchestrator._derive_prompt_tool_requirements(
        "Fully represent the corresponding paper from #V#file_copy_abc"
    )
    assert result.get("scholarly_representation_intent") is False


def test_derive_prompt_tool_requirements_no_verification_side_effects() -> None:
    """Concept-verification language no longer injects tool requirements."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    result = InternalMCPChatOrchestrator._derive_prompt_tool_requirements(
        "verify that #V#concept_abc exists in the ontology"
    )
    # Only explicit tool mentions should appear — not implicit verification
    required = result.get("required_tools") or []
    assert "concept_exists" not in required
    assert "fetch_concept" not in required


# ---- Decision authority annotations ------------------------------------

def test_annotate_python_decision_event_envelope() -> None:
    """annotate_python_decision_event produces the standard envelope."""
    from src.backend.services.python_decision_authority_service import (
        annotate_python_decision_event,
    )

    event = annotate_python_decision_event(
        {"type": "episode_evaluation_autotrigger_gate"},
        stage="completion_gate",
        component="internal_mcp_orchestrator",
        function="test_function",
        decision_class="episode_evaluation_autotrigger_gate",
        decision_source="gate_check",
        changed_outcome=False,
        reason_code="autotrigger_skipped",
        possible_inappropriate_python_code_use=False,
    )

    assert event["decision_authority_origin"] == "python"
    assert event["decision_class"] == "episode_evaluation_autotrigger_gate"
    assert event["decision_source"] == "gate_check"
    assert event["changed_outcome"] is False
    assert event["possible_inappropriate_python_code_use"] is False
    assert event["type"] == "episode_evaluation_autotrigger_gate"


def test_workflow_gap_recovery_annotation_envelope() -> None:
    """annotate_python_decision_event produces correct envelope for gap recovery."""
    from src.backend.services.python_decision_authority_service import (
        annotate_python_decision_event,
    )

    event = annotate_python_decision_event(
        {
            "type": "workflow_gap_recovery",
            "status": "applied",
            "workflow_id": "#V#test_workflow_gap",
        },
        stage="workflow_dispatch",
        component="internal_mcp_orchestrator",
        function="_maybe_apply_workflow_gap_recovery",
        decision_class="workflow_gap_recovery_gate",
        decision_source="gate_check",
        changed_outcome=True,
        reason_code="workflow_gap_recovery_applied",
        possible_inappropriate_python_code_use=False,
    )

    assert event["decision_authority_origin"] == "python"
    assert event["decision_class"] == "workflow_gap_recovery_gate"
    assert event["changed_outcome"] is True
    assert event["stage"] == "workflow_dispatch"
    assert event["status"] == "applied"


# ---- Phase 10: Presenter / completion fallback authority ----------------

def test_completion_ledger_injection_annotation_envelope() -> None:
    """Completion ledger injection produces scaffolding-tagged annotation."""
    from src.backend.services.python_decision_authority_service import (
        annotate_python_decision_event,
    )

    event = annotate_python_decision_event(
        {
            "type": "completion_ledger_injection",
            "decision": "failed",
            "ledger_replaced_empty_response": True,
            "decision_authority": {
                "origin": "python",
                "decision_class": "completion_ledger_injection",
                "scaffolding": True,
            },
        },
        stage="completion_gate",
        component="internal_mcp_orchestrator",
        function="_action_turn_execution_completion_gate",
        decision_class="completion_ledger_injection",
        decision_source="execution_postcondition_check",
        changed_outcome=True,
        reason_code="failed",
        possible_inappropriate_python_code_use=True,
    )

    assert event["decision_authority_origin"] == "python"
    assert event["decision_class"] == "completion_ledger_injection"
    assert event["possible_inappropriate_python_code_use"] is True
    assert event["decision_authority"]["scaffolding"] is True
    assert event["ledger_replaced_empty_response"] is True


def test_presenter_fallback_uses_structural_pattern_detection() -> None:
    """Presenter fallback decision_source is structural, not semantic."""
    from src.backend.services.python_decision_authority_service import (
        annotate_python_decision_event,
    )

    event = annotate_python_decision_event(
        {
            "type": "presenter_screen_backfill",
            "stage": "screen_backfill",
            "source": "follow_up_summary",
        },
        stage="screen_backfill",
        component="presenter_routes",
        function="_build_presenter_follow_up_summary_from_tool_messages",
        decision_class="presenter_fallback",
        decision_source="structural_pattern_detection",
        changed_outcome=True,
        reason_code="tool_backed_follow_up_summary",
        possible_inappropriate_python_code_use=True,
    )

    assert event["decision_source"] == "structural_pattern_detection"
    assert event["possible_inappropriate_python_code_use"] is True
    # structural_pattern_detection is NOT in _PROMPT_SEMANTIC_DECISION_SOURCES
    # but we explicitly set possible_inappropriate=True because it changes
    # user-visible meaning
    from src.backend.services.python_decision_authority_service import (
        _PROMPT_SEMANTIC_DECISION_SOURCES,
    )
    assert "structural_pattern_detection" not in _PROMPT_SEMANTIC_DECISION_SOURCES


def test_llm_screen_synthesis_annotation_envelope() -> None:
    """LLM screen synthesis produces a decision_authority annotation."""
    from src.backend.services.python_decision_authority_service import (
        annotate_python_decision_event,
    )

    event = annotate_python_decision_event(
        {
            "type": "presenter_screen_backfill",
            "stage": "screen_backfill",
            "source": "llm_synthesis",
            "diagnostic_rewrite": True,
        },
        stage="screen_backfill",
        component="presenter_routes",
        function="_presenter_llm_screen_synthesis",
        decision_class="presenter_fallback",
        decision_source="llm_synthesis",
        changed_outcome=True,
        reason_code="diagnostic_ledger_rewritten",
        possible_inappropriate_python_code_use=True,
    )

    assert event["decision_authority_origin"] == "python"
    assert event["decision_source"] == "llm_synthesis"
    assert event["reason_code"] == "diagnostic_ledger_rewritten"
    assert event["diagnostic_rewrite"] is True
