"""Phase 11 (JVNAUTOSCI-1619): regression coverage for workflow-authority refactor.

Detects both semantic authority escape (Python re-acquiring prompt-semantic
control) and capability loss (analytical/canonical artefact handling regresses).

Categories:
  1. Semantic authority escape — catches prompt-semantic pattern or decision source
     re-introduction in orchestrator code.
  2. Telemetry integrity — catches pseudo-progress annotations that do not reflect
     real behavioural control, and validates the auto-flag mechanism.
  3. Capability regression — catches breakage in arXiv ID/URL handling and ensures
     prompt tool requirement inference stays explicit-only.
"""

from __future__ import annotations

import inspect
import re

import pytest

# ---------------------------------------------------------------------------
# Category 1: Semantic authority escape detection
# ---------------------------------------------------------------------------


def test_no_prompt_semantic_regex_patterns_in_orchestrator() -> None:
    """Guard against re-introduction of _PROMPT_*_PATTERN regex attributes.

    Phase 9 removed four dead regex patterns that attempted to infer prompt
    intent via lexical matching.  Re-adding any such attribute is a
    semantic-authority regression.
    """
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    banned_prefixes = [
        "_PROMPT_CONCEPT_VERIFICATION_HINT_PATTERN",
        "_PROMPT_SCHOLARLY_REPRESENTATION_INTENT_PATTERN",
        "_PROMPT_PAPER_INTENT_PATTERN",
        "_PROMPT_REPRESENTATION_INTENT_PATTERN",
    ]

    for name in banned_prefixes:
        assert not hasattr(InternalMCPChatOrchestrator, name), (
            f"Prompt-semantic regex '{name}' was re-introduced — "
            "this is a workflow-authority regression.  Analytical intent "
            "detection must be workflow/LLM-owned."
        )

    # Scan for any NEW semantic-intent patterns (INTENT/HINT/SEMANTIC in name).
    # Explicit-identifier patterns (_PROMPT_EXPLICIT_TOOL_CALL_PATTERN,
    # _PROMPT_URL_PATTERN, etc.) are legitimate structural parsers.
    semantic_pattern_attrs = [
        attr
        for attr in dir(InternalMCPChatOrchestrator)
        if attr.startswith("_PROMPT_")
        and attr.endswith("_PATTERN")
        and any(
            kw in attr
            for kw in ("INTENT", "HINT", "SEMANTIC", "INFERENCE", "ANALYTICAL")
        )
    ]
    assert semantic_pattern_attrs == [], (
        f"New prompt-semantic regex patterns detected: {semantic_pattern_attrs}. "
        "All prompt-intent inference must be workflow/LLM-owned, not Python regex."
    )


def test_no_python_lexical_selector_guidance_in_policy_service() -> None:
    """Selector candidate steering must not fall back to Python lexical logic."""
    from src.backend.services import workflow_selection_policy_service as mod

    banned_attributes = [
        "_LEXICAL_GUIDANCE_STOPWORDS",
        "_normalise_selector_text",
        "_filtered_selector_tokens",
        "_selector_query_phrases",
        "_recommend_guidance_candidate_by_specificity",
    ]
    for name in banned_attributes:
        assert not hasattr(mod, name), (
            f"{name} was re-introduced in workflow_selection_policy_service. "
            "Selector semantic guidance must remain LLM- or learned-policy-owned."
        )

    source = inspect.getsource(mod)
    banned_fragments = [
        "lexical_specificity",
        "lexical_guidance",
        "lexical_candidate_scores",
        "selection_reason\": \"lexical_specificity_guidance_candidate",
    ]
    for fragment in banned_fragments:
        assert fragment not in source, (
            f"Found banned lexical selector guidance fragment '{fragment}' in "
            "workflow_selection_policy_service. Python must not steer workflow "
            "selection semantics via lexical overlap."
        )


def test_prompt_semantic_source_budget_in_orchestrator() -> None:
    """Only one orchestrator call site should use prompt_semantic_inference.

    Currently: ontology_preflight.  If more call sites appear, they need
    explicit justification and this budget must be updated.
    """
    from src.backend.integrations.internal_mcp import orchestrator as mod
    from src.backend.services.python_decision_authority_service import (
        _PROMPT_SEMANTIC_DECISION_SOURCES,
    )

    source = inspect.getsource(mod)

    # Count literal string references to any _PROMPT_SEMANTIC_DECISION_SOURCES value
    # in decision_source= keyword argument context
    hits: list[str] = []
    for semantic_source in sorted(_PROMPT_SEMANTIC_DECISION_SOURCES):
        # Look for the literal string as a decision_source value
        pattern = rf'decision_source\s*=\s*["\']({re.escape(semantic_source)})["\']'
        matches = re.findall(pattern, source)
        hits.extend(matches)

    # Budget: exactly 1 (ontology_preflight → prompt_semantic_inference)
    assert len(hits) <= 1, (
        f"Expected ≤1 prompt-semantic decision_source in orchestrator, "
        f"found {len(hits)}: {hits}.  New prompt-semantic call sites need "
        "explicit justification or migration to workflow-owned logic."
    )


def test_derive_prompt_tool_requirements_no_url_extraction_for_scholarly_text() -> None:
    """Analytical/scholarly prompts must NOT trigger URL extraction tools."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    scholarly_prompts = [
        "Fully represent the paper from https://arxiv.org/abs/2301.12345",
        "What are the key contributions of this paper?",
        "Analyse the methodology used in the study of transformer architectures",
        "Summarise the findings from the linked research",
    ]

    for prompt in scholarly_prompts:
        result = InternalMCPChatOrchestrator._derive_prompt_tool_requirements(prompt)
        assert result.get("required_url_extraction_tool") is None, (
            f"Prompt '{prompt[:50]}…' triggered URL extraction tool inference — "
            "this must be workflow/LLM-owned."
        )
        assert result.get("scholarly_representation_intent") is False, (
            f"scholarly_representation_intent was not False for: '{prompt[:50]}…'"
        )


# ---------------------------------------------------------------------------
# Category 2: Telemetry integrity (pseudo-progress detection)
# ---------------------------------------------------------------------------


def test_each_prompt_semantic_source_auto_flags_inappropriate() -> None:
    """Every _PROMPT_SEMANTIC_DECISION_SOURCES value must auto-flag.

    If possible_inappropriate_python_code_use is not explicitly set,
    annotate_python_decision_event must infer True for these sources.
    Regression in this logic would silently under-count authority escape.
    """
    from src.backend.services.python_decision_authority_service import (
        _PROMPT_SEMANTIC_DECISION_SOURCES,
        annotate_python_decision_event,
    )

    for source in sorted(_PROMPT_SEMANTIC_DECISION_SOURCES):
        event = annotate_python_decision_event(
            {},
            stage="test",
            component="test",
            function="test",
            decision_class="test",
            decision_source=source,
            changed_outcome=False,
            # Deliberately omit possible_inappropriate_python_code_use
        )
        assert event["possible_inappropriate_python_code_use"] is True, (
            f"decision_source='{source}' did not auto-flag "
            "possible_inappropriate_python_code_use — the auto-flag "
            "mechanism is broken."
        )


def test_non_semantic_sources_not_auto_flagged() -> None:
    """Non-semantic decision sources must NOT be auto-flagged inappropriate.

    Sources like 'gate_check', 'execution_postcondition_check', and
    'structural_pattern_detection' represent legitimate Python safety
    infrastructure, not semantic inference.
    """
    from src.backend.services.python_decision_authority_service import (
        annotate_python_decision_event,
    )

    safe_sources = [
        "gate_check",
        "execution_postcondition_check",
        "retry_budget_check",
        "response_structure_check",
        "structural_pattern_detection",
        "execution_safety_check",
        "workflow_retry_prompt",
        "workflow_retry_response",
        "stall_detection",
        "workflow_launchability_check",
        "workflow_authored_policy",
        "explicit_identifier_parse",
    ]

    for source in safe_sources:
        event = annotate_python_decision_event(
            {},
            stage="test",
            component="test",
            function="test",
            decision_class="test",
            decision_source=source,
            changed_outcome=False,
        )
        assert event["possible_inappropriate_python_code_use"] is False, (
            f"Non-semantic source '{source}' was auto-flagged as inappropriate — "
            "this source should not be in _PROMPT_SEMANTIC_DECISION_SOURCES."
        )


def test_build_stage_authority_summary_counts_inappropriate_decisions() -> None:
    """Stage summary must count possibly-inappropriate decisions accurately.

    If the counting logic drifts (e.g., stops checking the flag), telemetry
    becomes pseudo-progress — numbers go up but visibility into authority
    escape decreases.
    """
    from src.backend.services.python_decision_authority_service import (
        annotate_python_decision_event,
        build_stage_authority_summary,
    )

    entries = [
        annotate_python_decision_event(
            {},
            stage="test_stage",
            component="test",
            function="test",
            decision_class="safe_gate",
            decision_source="gate_check",
            changed_outcome=False,
        ),
        annotate_python_decision_event(
            {},
            stage="test_stage",
            component="test",
            function="test",
            decision_class="semantic_inference",
            decision_source="prompt_semantic_inference",
            changed_outcome=True,
        ),
        annotate_python_decision_event(
            {},
            stage="test_stage",
            component="test",
            function="test",
            decision_class="another_safe",
            decision_source="execution_postcondition_check",
            changed_outcome=False,
        ),
    ]

    summary = build_stage_authority_summary(
        stage_id="test_stage",
        aux_entries=entries,
    )

    assert summary["python_decision_count"] == 3
    assert summary["possibly_inappropriate_python_code_use_count"] == 1
    assert "semantic_inference" in summary["python_decision_classes"]
    assert "prompt_semantic_inference" in summary["python_decision_sources"]


def test_build_stage_authority_summary_preserves_bounded_llm_exchange_summaries() -> None:
    from src.backend.services.python_decision_authority_service import (
        build_stage_authority_summary,
    )

    entries = [
        {
            "type": "workflow_selector_prompt",
            "prompt_id": "#V#chat_turn_classifier_prompt",
            "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
            "prompt": {"text": "Select workflow", "char_count": 15},
            "candidate_list": {
                "text": "- #V#tool_calling_workflow",
                "char_count": 24,
            },
            "candidate_entries": [
                {
                    "concept_id": "#V#chat_assistant_workflow",
                    "candidate_source": "selector_default",
                },
                {
                    "concept_id": "#V#tool_calling_workflow",
                    "candidate_source": "selector_default",
                },
            ],
        },
        {
            "type": "workflow_model_policy_stage",
            "stage": "workflow_dispatch",
            "policy_stage": "classifier",
            "requested_model": "gpt-5.4-mini",
            "selection_mode": "policy_primary_override",
            "follows_active_llm": False,
            "explicit_stage_model_override": True,
            "explicit_stage_model_override_origin": "policy_primary",
            "request": {
                "prompt": {"text": "Select workflow", "char_count": 15},
                "context_message_count": 1,
            },
            "selected": {
                "provider": "openai",
                "model": "gpt-5-mini",
                "model_resolved": "gpt-5-mini",
            },
            "fallback_used": True,
            "fallback_attempt_count": 2,
            "failure_count": 1,
            "fallback_attempts": [
                {
                    "attempt_no": 1,
                    "provider": "ollama",
                    "model": "granite3.3:2b",
                    "status": "failed",
                    "failure_kind": "provider_unreachable",
                },
                {
                    "attempt_no": 2,
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "status": "succeeded",
                    "response": {
                        "text": "#V#tool_calling_workflow",
                        "char_count": 24,
                    },
                },
            ],
            "errors": [
                {
                    "failure_kind": "provider_unreachable",
                    "error": "connection refused",
                }
            ],
        },
        {
            "type": "workflow_selector",
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "selection_source": "selector",
            "response": {
                "text": "#V#tool_calling_workflow",
                "char_count": 24,
            },
            "selection_metadata": {
                "selection_resolution": "candidate_label_exact_match",
            },
        },
    ]

    summary = build_stage_authority_summary(
        stage_id="workflow_dispatch",
        aux_entries=entries,
    )

    assert summary["llm_exchange_record_count"] == 3
    assert summary["llm_exchange_entry_types"] == [
        "workflow_selector_prompt",
        "workflow_model_policy_stage",
        "workflow_selector",
    ]
    assert summary["llm_exchange_summary_truncated_count"] == 0
    assert summary["llm_exchange_summaries"][0]["prompt_id"] == (
        "#V#chat_turn_classifier_prompt"
    )
    assert summary["llm_exchange_summaries"][1]["selected_model"] == "gpt-5-mini"
    assert summary["llm_exchange_summaries"][1]["requested_model"] == "gpt-5.4-mini"
    assert summary["llm_exchange_summaries"][1]["selection_mode"] == (
        "policy_primary_override"
    )
    assert summary["llm_exchange_summaries"][1]["explicit_stage_model_override"] is True
    assert summary["llm_exchange_summaries"][1]["failure_kinds"] == [
        "provider_unreachable"
    ]
    assert summary["llm_exchange_summaries"][1]["response_preview"]["text"] == (
        "#V#tool_calling_workflow"
    )
    assert summary["latest_llm_exchange"]["entry_type"] == "workflow_selector"
    assert summary["latest_llm_exchange"]["response_preview"]["text"] == (
        "#V#tool_calling_workflow"
    )


# ---------------------------------------------------------------------------
# Category 3: Capability regression — arXiv and analytical prompts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_id",
    [
        # Standard abs URL
        ("https://arxiv.org/abs/2301.12345", "2301.12345"),
        # PDF URL
        ("https://arxiv.org/pdf/2301.12345", "2301.12345"),
        # With version suffix (should be stripped)
        ("https://arxiv.org/abs/2301.12345v2", "2301.12345"),
        # arxiv: prefix notation
        ("arxiv:2301.12345", "2301.12345"),
        # Bare new-style ID
        ("2301.12345", "2301.12345"),
        # Old-style category ID
        ("hep-ph/0301234", "hep-ph/0301234"),
        # Old-style category URL
        ("https://arxiv.org/abs/hep-ph/0301234", "hep-ph/0301234"),
        # With version on old-style
        ("https://arxiv.org/abs/hep-ph/0301234v1", "hep-ph/0301234"),
        # Embedded in text
        ("Please look at https://arxiv.org/abs/2301.12345 for details", "2301.12345"),
        # arxiv: prefix embedded
        ("Check arxiv:2301.12345 for the paper", "2301.12345"),
    ],
    ids=[
        "abs_url",
        "pdf_url",
        "versioned_url",
        "arxiv_prefix",
        "bare_new_style",
        "old_style_category",
        "old_style_url",
        "old_style_versioned",
        "embedded_in_text",
        "prefix_embedded",
    ],
)
def test_arxiv_id_extraction_canonical_formats(text: str, expected_id: str) -> None:
    """ArXiv ID extraction must handle all canonical formats.

    Capability regression here means users sending arXiv URLs/IDs would
    not get proper paper download/analysis.
    """
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    result = InternalMCPChatOrchestrator._extract_arxiv_id_from_text(text)
    assert result == expected_id, (
        f"ArXiv extraction failed for '{text}': got '{result}', "
        f"expected '{expected_id}'"
    )


def test_arxiv_id_extraction_returns_none_for_non_arxiv() -> None:
    """Non-arXiv text must return None, not a false positive."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    non_arxiv = [
        "What is the meaning of life?",
        "https://example.com/paper.pdf",
        "The number 42 is important",
        "",
        "   ",
    ]

    for text in non_arxiv:
        result = InternalMCPChatOrchestrator._extract_arxiv_id_from_text(text)
        assert result is None, (
            f"False positive arXiv ID from non-arXiv text: '{text}' → '{result}'"
        )


def test_explicit_tool_extraction_for_analytical_prompts() -> None:
    """Prompts mentioning tools explicitly must still extract them.

    Capability regression: the switch to explicit-only must not break
    tool extraction for users who name tools directly in their prompts.
    """
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    result = InternalMCPChatOrchestrator._derive_prompt_tool_requirements(
        "Use search_concepts to find all predicates related to causation"
    )
    required = result.get("required_tools") or []
    assert "search_concepts" in required, (
        "Explicit tool mention in prompt was not extracted — "
        "explicit-only tool extraction is broken."
    )


def test_write_continuation_prompt_pattern_removed() -> None:
    """The former _WRITE_CONTINUATION_PROMPT_PATTERN dead code is gone.

    This regex was removed during the authority refactor (Phase 1/2).
    Re-introduction would be a semantic authority regression.
    """
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    assert not hasattr(
        InternalMCPChatOrchestrator,
        "_WRITE_CONTINUATION_PROMPT_PATTERN",
    ), (
        "_WRITE_CONTINUATION_PROMPT_PATTERN was re-introduced — "
        "write continuation is workflow-owned."
    )


def test_prompt_semantic_decision_sources_is_frozen() -> None:
    """_PROMPT_SEMANTIC_DECISION_SOURCES must be immutable.

    Modification at runtime would silently change the auto-flag boundary.
    """
    from src.backend.services.python_decision_authority_service import (
        _PROMPT_SEMANTIC_DECISION_SOURCES,
    )

    assert isinstance(_PROMPT_SEMANTIC_DECISION_SOURCES, frozenset), (
        "_PROMPT_SEMANTIC_DECISION_SOURCES must be a frozenset to prevent "
        "runtime mutation of the semantic-authority boundary."
    )

    expected_contents = {
        "prompt_semantic_inference",
        "prompt_shape_heuristic",
        "lexical_heuristic",
        "response_semantic_inference",
        "semantic_veto",
        "semantic_override",
    }
    assert _PROMPT_SEMANTIC_DECISION_SOURCES == expected_contents, (
        f"_PROMPT_SEMANTIC_DECISION_SOURCES contents changed. "
        f"Expected: {expected_contents}, got: {_PROMPT_SEMANTIC_DECISION_SOURCES}. "
        "Changes to the semantic boundary need explicit justification."
    )
