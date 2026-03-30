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
