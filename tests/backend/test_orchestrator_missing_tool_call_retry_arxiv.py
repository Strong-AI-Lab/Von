from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    _MissingToolCallDetectorSpec,
)
from src.backend.workflows.turn_expected_outcome_contract import (
    build_turn_expected_outcome_boundary_payload,
)

_TEST_CLASSIFIER_PROMPT = "Answer YES or NO for: {response}"


def _build_structured_turn_contract_payload(
    *,
    summary: str | None = None,
    grounding_requirement: str | None = None,
    precision_policy: str | None = None,
    selector_guidance: str | None = None,
    answering_guidance: str | None = None,
    reasoning: str | None = None,
    required_tools: Sequence[str] = (),
) -> dict[str, Any]:
    contract: dict[str, Any] = {}
    for field_name, value in (
        ("summary", summary),
        ("grounding_requirement", grounding_requirement),
        ("precision_policy", precision_policy),
        ("selector_guidance", selector_guidance),
        ("answering_guidance", answering_guidance),
        ("reasoning", reasoning),
    ):
        if isinstance(value, str) and value.strip():
            contract[field_name] = value.strip()
    tools = [
        str(item).strip()
        for item in required_tools
        if isinstance(item, str) and str(item).strip()
    ]
    if tools:
        contract["required_tools"] = tools
    return build_turn_expected_outcome_boundary_payload(contract)


class _Gateway:
    @staticmethod
    def describe_methods() -> dict[str, Any]:
        return {
            "search_concepts": {
                "category": "read",
                "description": "Search represented concepts by name and description",
            },
            "vontology_concept_search": {
                "category": "read",
                "description": "Search represented concepts by name and description",
            },
            "search_knowledge_base": {
                "category": "read",
                "description": "Semantic search over represented knowledge",
            },
            "search_web": {
                "category": "read",
                "description": "Search the public web",
            },
            "search_arxiv": {
                "category": "read",
                "description": "Search arXiv papers",
            },
            "jira_search": {
                "category": "read",
                "description": "Search Jira issues using JQL",
            },
            "get_text_relations_summary": {
                "category": "read",
                "description": "Summarise text relations for a concept",
            },
            "get_related_concepts": {
                "category": "read",
                "description": "Retrieve related concepts for a concept",
            },
            "find_relations_with_argument": {
                "category": "read",
                "description": "Retrieve relation hits for a concept argument",
            },
            "get_predicate_incidence": {
                "category": "read",
                "description": "Summarise predicate incidence for an entity or type",
            },
            "task_create": {
                "category": "write",
                "description": "Create a Von task",
            },
            "task_search": {
                "category": "read",
                "description": "Search Von tasks",
            },
            "download_paper": {
                "category": "write",
                "description": "Download an arXiv paper and store as an artefact",
            },
            "finalise_cached_paper": {
                "category": "write",
                "description": "Upload cached arXiv PDF and register file copy",
            },
            "materialise_scholarly_representation_for_file_copy": {
                "category": "write",
                "description": "Materialise scholarly-paper representation from file copy",
            },
            "list_papers": {
                "category": "read",
                "description": "List cached papers",
            },
        }


class _CapturingLLM:
    def __init__(self, responses: Sequence[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model=None,
    ):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if not self._responses:
            raise AssertionError("LLM called more times than expected")
        return self._responses.pop(0)


def _build_orchestrator_stub() -> InternalMCPChatOrchestrator:
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._logger = logging.getLogger(__name__)
    orchestrator._gateway = cast(Any, _Gateway())
    orchestrator._follow_up_context_chars = 4000
    orchestrator._max_missing_tool_call_retries_per_turn = 3
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="fallback_missing_tool_call_detector",
        prompt_id=None,
        prompt_text=_TEST_CLASSIFIER_PROMPT,
        model=None,
    )
    return orchestrator


def _uses_missing_tool_call_classifier_prompt(
    calls: Sequence[Mapping[str, Any]],
    classifier_prompt: str,
) -> bool:
    classifier_prefix = classifier_prompt.split("{response}", 1)[0].strip()
    for call in calls:
        prompt = call.get("prompt")
        if not isinstance(prompt, str):
            continue
        if classifier_prefix and classifier_prefix in prompt:
            return True
    return False


def test_missing_tool_call_assessment_uses_classifier_when_detector_is_available():
    orchestrator = _build_orchestrator_stub()

    llm = _CapturingLLM(["YES"])

    response_text = "Here is the actual tool call."
    assessment = orchestrator._assess_missing_tool_call(
        response_text=response_text,
        use_structured=False,
        interpretation=orchestrator._interpret_model_turn(response_text),
        llm_client=llm,
        model=None,
        classifier_model=None,
        aux_log=[],
        tool_call_parse_error=None,
        allow_semantic_retry=True,
    )

    assert assessment.retry_reason == "LLM classifier flagged missing tool call"
    assert assessment.classifier_invoked is True
    assert _uses_missing_tool_call_classifier_prompt(llm.calls, _TEST_CLASSIFIER_PROMPT)


def test_missing_tool_call_retry_does_not_force_domain_specific_download_tool():
    orchestrator = _build_orchestrator_stub()

    prompt = "Download arXiv:2506.16596 and store it as an artefact."
    requirements = orchestrator._derive_prompt_tool_requirements(
        prompt,
        method_catalogue=_Gateway.describe_methods(),
        context_messages=[],
    )
    assert requirements["required_tools"] == []

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        missing_required_tools=["download_paper"],
    )
    assert forced is None


def test_missing_tool_call_retry_does_not_force_domain_specific_finalise_tool():
    orchestrator = _build_orchestrator_stub()

    prompt = "Finalise cached arXiv:2506.16596v2 and store it as an artefact."
    requirements = orchestrator._derive_prompt_tool_requirements(
        prompt,
        method_catalogue=_Gateway.describe_methods(),
        context_messages=[],
    )
    assert requirements["required_tools"] == []

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        missing_required_tools=["finalise_cached_paper"],
    )
    assert forced is None


def test_missing_tool_call_retry_chains_text_relation_summary_after_search_concepts():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What predicates are salient to SAIL students?",
        missing_required_tools=["get_text_relations_summary"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#sail_student_group",
                            "name": "SAIL Student Group",
                            "relevance_score": 98.0,
                        },
                        {
                            "concept_id": "#V#unrelated_concept",
                            "name": "Unrelated Concept",
                            "relevance_score": 55.0,
                        },
                    ],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#sail_student_group"},
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {"concept_id": "#V#sail_student_group", "limit": 20},
        },
    ]


def test_missing_tool_call_retry_uses_workflow_recovery_contract_binding():
    orchestrator = _build_orchestrator_stub()
    contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "grounded_entity_information_retrieval_evidence",
        "required_effects": [
            {
                "effect_id": "grounded_entity_information_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": ["get_text_relations_summary"],
                "recovery_strategies": [
                    {
                        "strategy_id": "recover_text_relations_for_focal_entity",
                        "tool": "get_text_relations_summary",
                        "recovers_tools": ["get_text_relations_summary"],
                        "target_concept_source": "required_fetch_or_focal_concept",
                        "target_concept_argument_name": "concept_id",
                    }
                ],
            }
        ],
    }

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="Who am I?",
        turn_expected_outcome_contract={
            "target_concept_ids": ["#V#michael_witbrock"],
        },
        workflow_required_effects_contract=contract,
        workflow_required_effects_contract_source="definition_metadata",
        missing_required_tools=["get_text_relations_summary"],
        missing_required_fetch_concept_ids=["#V#michael_witbrock"],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#michael_witbrock"},
            "_retry_binding_source": "workflow_recovery_contract",
            "_retry_target_concept_source": "required_fetch_or_focal_concept",
            "_retry_recovery_contract_id": (
                "grounded_entity_information_retrieval_evidence"
            ),
            "_retry_recovery_contract_source": "definition_metadata",
            "_retry_recovery_effect_id": "grounded_entity_information_evidence",
            "_retry_recovery_strategy_id": (
                "recover_text_relations_for_focal_entity"
            ),
        }
    ]
    assert orchestrator._summarise_retry_tool_call_binding_sources(
        cast(list[Mapping[str, Any]], forced)
    ) == [
        {
            "tool": "get_text_relations_summary",
            "binding_source": "workflow_recovery_contract",
            "target_concept_source": "required_fetch_or_focal_concept",
            "recovery_contract_id": (
                "grounded_entity_information_retrieval_evidence"
            ),
            "recovery_contract_source": "definition_metadata",
            "recovery_effect_id": "grounded_entity_information_evidence",
            "recovery_strategy_id": "recover_text_relations_for_focal_entity",
        }
    ]


def test_workflow_recovery_contract_blocks_legacy_follow_up_without_strategy():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What predicates are salient to SAIL students?",
        workflow_required_effects_contract={
            "schema_version": "workflow_required_effects_contract.v1",
            "contract_id": "grounded_entity_information_retrieval_evidence",
            "required_effects": [
                {
                    "effect_id": "grounded_entity_information_evidence",
                    "effect_type": "grounded_evidence",
                    "required_tools": ["get_text_relations_summary"],
                }
            ],
        },
        workflow_required_effects_contract_source="definition_metadata",
        missing_required_tools=["get_text_relations_summary"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#sail_student_group",
                            "name": "SAIL Student Group",
                            "relevance_score": 98.0,
                        },
                    ],
                },
            }
        ],
    )

    assert forced is None


def test_missing_tool_call_retry_chains_related_concepts_after_search_concepts():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What related concepts should I inspect for SAIL students?",
        missing_required_tools=["get_related_concepts"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#sail_student_group",
                            "name": "SAIL Student Group",
                            "relevance_score": 97.0,
                        }
                    ],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_related_concepts",
            "payload": {"concept_id": "#V#sail_student_group"},
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {"concept_id": "#V#sail_student_group", "limit": 20},
        },
    ]


def test_missing_tool_call_retry_chains_fetch_concept_after_search_concepts():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What predicates are represented for scientific papers?",
        missing_required_tools=["fetch_concept"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#scientific_paper",
                            "name": "scientific paper",
                            "kind": "type",
                        }
                    ],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "fetch_concept",
            "payload": {"concept_id": "#V#scientific_paper"},
        }
    ]


def test_missing_tool_call_retry_chains_predicate_incidence_after_search_concepts():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What predicates are represented for scientific papers?",
        missing_required_tools=["get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#scientific_paper",
                            "name": "scientific paper",
                            "kind": "type",
                        }
                    ],
                },
            },
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "effective_payload": {"success": True},
            },
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_predicate_incidence",
            "payload": {"concept_id": "#V#scientific_paper"},
        }
    ]


def test_missing_tool_call_retry_prefers_search_target_over_actor_predicate_filter():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for scientific papers in Vontology?",
        missing_required_tools=["get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "query": "scientific paper",
                    "results": [
                        {
                            "concept_id": "#V#has_paper_recommendation_assertion",
                            "name": "Has Paper Recommendation Assertion",
                            "kind": "predicate",
                        },
                        {
                            "concept_id": "#V#scientific_paper",
                            "name": "scientific paper",
                            "kind": "type",
                        },
                    ],
                },
            },
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {
                    "concept_id": "#V#michael_witbrock",
                    "predicate_filter": ["#V#has_paper_recommendation_assertion"],
                },
                "effective_payload": {"success": True},
            },
        ],
        user_concept_id="#V#michael_witbrock",
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_predicate_incidence",
            "payload": {"concept_id": "#V#scientific_paper"},
        }
    ]


def test_missing_tool_call_retry_chains_predicate_incidence_after_fetch_concept():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for scientific papers in Vontology?",
        missing_required_tools=["get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "fetch_concept",
                "status": "ok",
                "arguments": {"concept_id": "#V#paper_on_arxiv_2603_01896"},
            },
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "effective_payload": {"success": True},
            },
        ],
        user_concept_id="#V#michael_witbrock",
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_predicate_incidence",
            "payload": {"concept_id": "#V#paper_on_arxiv_2603_01896"},
        }
    ]


def test_missing_tool_call_retry_fetches_canonical_search_query_before_actor_incidence():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for papers in Vontology?",
        missing_required_tools=["get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "arguments": {"query": "paper"},
            },
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "effective_payload": {"success": True},
            },
        ],
        user_concept_id="#V#michael_witbrock",
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "fetch_concept",
            "payload": {"concept_id": "#V#paper"},
        }
    ]


def test_missing_tool_call_retry_fetches_search_query_before_actor_predicate_filter():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for scientific papers in Vontology?",
        missing_required_tools=["get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "query": "scientific paper",
                    "results": [
                        {
                            "concept_id": "#V#has_paper_recommendation_assertion",
                            "name": "Has Paper Recommendation Assertion",
                            "kind": "predicate",
                        }
                    ],
                },
            },
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {
                    "concept_id": "#V#michael_witbrock",
                    "predicate_filter": ["#V#has_paper_recommendation_assertion"],
                },
                "effective_payload": {"success": True},
            },
        ],
        user_concept_id="#V#michael_witbrock",
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "fetch_concept",
            "payload": {"concept_id": "#V#scientific_paper"},
        }
    ]


def test_missing_tool_call_retry_chains_relation_summary_after_vontology_search_alias():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for scientific papers in Vontology?",
        missing_required_tools=["get_text_relations_summary"],
        tool_invocations=[
            {
                "tool": "vontology_concept_search",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "query": "scientific paper",
                    "results": [
                        {
                            "concept_id": "#V#scientific_paper",
                            "name": "scientific paper",
                            "kind": "type",
                        }
                    ],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#scientific_paper"},
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {"concept_id": "#V#scientific_paper", "limit": 20},
        },
    ]


def test_missing_tool_requirements_treat_vontology_search_as_search_concepts_alias():
    missing_tools, _, _, _ = (
        InternalMCPChatOrchestrator._derive_missing_prompt_requirements(
            required_tools=["search_concepts", "get_text_relations_summary"],
            required_fetch_concept_ids=[],
            tool_invocations=[
                {
                    "tool": "vontology_concept_search",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "results": [
                            {
                                "concept_id": "#V#scientific_paper",
                                "name": "scientific paper",
                                "kind": "type",
                            }
                        ],
                    },
                }
            ],
        )
    )

    assert missing_tools == ["get_text_relations_summary"]


def test_missing_tool_call_retry_forces_search_before_actor_predicate_incidence():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for scientific papers in Vontology?",
        turn_expected_outcome_contract={
            "selector_guidance": (
                "First identify the concept ID for 'scientific paper', then inspect "
                "predicate incidence."
            ),
            "required_tools": ["search_concepts", "get_predicate_incidence"],
        },
        missing_required_tools=["search_concepts", "get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "effective_payload": {"success": True},
            }
        ],
        user_concept_id="#V#michael_witbrock",
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "search_concepts",
            "payload": {"query": "scientific paper"},
        }
    ]


def test_missing_tool_call_retry_forces_resolve_from_quoted_turn_contract():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for scientific papers in Vontology?",
        turn_expected_outcome_contract={
            "selector_guidance": (
                "Resolve the 'scientific paper' concept before predicate lookup."
            ),
            "required_tools": [
                "resolve_concept_by_name",
                "get_predicate_incidence",
            ],
        },
        missing_required_tools=["resolve_concept_by_name", "get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "effective_payload": {"success": True},
            }
        ],
        user_concept_id="#V#michael_witbrock",
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "resolve_concept_by_name",
            "payload": {"name": "scientific paper", "max_results": 5},
        }
    ]


def test_missing_tool_call_retry_chains_predicate_incidence_after_resolution():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for scientific papers in Vontology?",
        turn_expected_outcome_contract={
            "required_tools": [
                "resolve_concept_by_name",
                "get_predicate_incidence",
            ],
        },
        missing_required_tools=["get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "resolve_concept_by_name",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "status": "resolved",
                    "resolved_concept_id": "#V#scientific_paper",
                },
            }
        ],
        user_concept_id="#V#michael_witbrock",
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_predicate_incidence",
            "payload": {"concept_id": "#V#scientific_paper"},
        }
    ]


def test_missing_tool_call_retry_searches_after_resolution_returns_no_target():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates for scientific papers in Vontology?",
        turn_expected_outcome_contract={
            "selector_guidance": (
                "Resolve the 'scientific paper' concept before predicate lookup."
            ),
            "required_tools": [
                "resolve_concept_by_name",
                "get_predicate_incidence",
            ],
        },
        missing_required_tools=["get_predicate_incidence"],
        tool_invocations=[
            {
                "tool": "resolve_concept_by_name",
                "status": "ok",
                "arguments": {"name": "scientific paper"},
                "effective_payload": {
                    "success": True,
                    "status": "not_found",
                    "summary": "Not found",
                },
            }
        ],
        user_concept_id="#V#michael_witbrock",
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "search_concepts",
            "payload": {"query": "scientific paper"},
        }
    ]


def test_missing_tool_requirements_keep_targeted_incidence_missing_until_resolved():
    missing_tools, _, _, _ = (
        InternalMCPChatOrchestrator._derive_missing_prompt_requirements(
            required_tools=["resolve_concept_by_name", "get_predicate_incidence"],
            required_fetch_concept_ids=[],
            tool_invocations=[
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#michael_witbrock"},
                }
            ],
        )
    )
    assert missing_tools == ["resolve_concept_by_name", "get_predicate_incidence"]

    missing_tools, _, _, _ = (
        InternalMCPChatOrchestrator._derive_missing_prompt_requirements(
            required_tools=["resolve_concept_by_name", "get_predicate_incidence"],
            required_fetch_concept_ids=[],
            tool_invocations=[
                {
                    "tool": "resolve_concept_by_name",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "resolved_concept_id": "#V#scientific_paper",
                    },
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#michael_witbrock"},
                },
            ],
        )
    )
    assert missing_tools == ["get_predicate_incidence"]

    missing_tools, _, _, _ = (
        InternalMCPChatOrchestrator._derive_missing_prompt_requirements(
            required_tools=["resolve_concept_by_name", "get_predicate_incidence"],
            required_fetch_concept_ids=[],
            tool_invocations=[
                {
                    "tool": "resolve_concept_by_name",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "resolved_concept_id": "#V#scientific_paper",
                    },
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#scientific_paper"},
                },
            ],
        )
    )
    assert missing_tools == []


def test_missing_tool_requirements_keep_search_bound_incidence_missing_until_targeted():
    missing_tools, _, _, _ = (
        InternalMCPChatOrchestrator._derive_missing_prompt_requirements(
            required_tools=["search_concepts", "get_predicate_incidence"],
            required_fetch_concept_ids=[],
            tool_invocations=[
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "results": [
                            {
                                "concept_id": "#V#scientific_paper",
                                "name": "scientific paper",
                                "kind": "type",
                            }
                        ],
                    },
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#michael_witbrock"},
                },
            ],
        )
    )
    assert missing_tools == ["get_predicate_incidence"]

    missing_tools, _, _, _ = (
        InternalMCPChatOrchestrator._derive_missing_prompt_requirements(
            required_tools=["search_concepts", "get_predicate_incidence"],
            required_fetch_concept_ids=[],
            tool_invocations=[
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "results": [
                            {
                                "concept_id": "#V#scientific_paper",
                                "name": "scientific paper",
                                "kind": "type",
                            }
                        ],
                    },
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#scientific_paper"},
                },
            ],
        )
    )
    assert missing_tools == []


def test_missing_tool_requirements_keep_fetch_bound_incidence_missing_until_targeted():
    missing_tools, _, _, _ = (
        InternalMCPChatOrchestrator._derive_missing_prompt_requirements(
            required_tools=[
                "search_concepts",
                "fetch_concept",
                "get_predicate_incidence",
            ],
            required_fetch_concept_ids=[],
            tool_invocations=[
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "arguments": {"query": "scientific paper"},
                },
                {
                    "tool": "fetch_concept",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#paper_on_arxiv_2603_01896"},
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#michael_witbrock"},
                },
            ],
        )
    )
    assert missing_tools == ["get_predicate_incidence"]

    missing_tools, _, _, _ = (
        InternalMCPChatOrchestrator._derive_missing_prompt_requirements(
            required_tools=[
                "search_concepts",
                "fetch_concept",
                "get_predicate_incidence",
            ],
            required_fetch_concept_ids=[],
            tool_invocations=[
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "arguments": {"query": "scientific paper"},
                },
                {
                    "tool": "fetch_concept",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#paper_on_arxiv_2603_01896"},
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#paper_on_arxiv_2603_01896"},
                },
            ],
        )
    )
    assert missing_tools == []


def test_missing_tool_call_retry_skips_file_copy_result_for_ontology_follow_up():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates or represented relationships for SAIL students?",
        missing_required_tools=["get_text_relations_summary"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#uploaded_file_copy_abc123",
                            "name": "Uploaded File Copy",
                            "kind": "individual",
                            "relevance_score": 99.0,
                        },
                        {
                            "concept_id": "#V#sail_student_group",
                            "name": "SAIL Student Group",
                            "kind": "type",
                            "relevance_score": 97.0,
                        },
                    ],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#sail_student_group"},
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {"concept_id": "#V#sail_student_group", "limit": 20},
        },
    ]


def test_missing_tool_call_retry_skips_predicate_meta_result_for_ontology_follow_up():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What predicates are salient to SAIL students?",
        missing_required_tools=["get_related_concepts"],
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#binary_predicate",
                            "name": "Binary Predicate",
                            "kind": "type",
                            "relevance_score": 99.0,
                            "hierarchy": {
                                "primary_path": [
                                    "#V#thing",
                                    "#V#predicate",
                                    "#V#binary_predicate",
                                ]
                            },
                        },
                        {
                            "concept_id": "#V#current_uo_asail_ph_d_student",
                            "name": "Current UoA SAIL PhD Student",
                            "kind": "type",
                            "relevance_score": 95.0,
                            "hierarchy": {
                                "primary_path": [
                                    "#V#thing",
                                    "#V#person",
                                    "#V#student",
                                ]
                            },
                        },
                    ],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_related_concepts",
            "payload": {"concept_id": "#V#current_uo_asail_ph_d_student"},
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {
                "concept_id": "#V#current_uo_asail_ph_d_student",
                "limit": 20,
            },
        },
    ]


def test_missing_tool_call_retry_recovers_search_concepts_from_prior_kb_query():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What are key predicates or represented relationships for SAIL students?",
        missing_required_tools=["search_concepts", "get_text_relations_summary"],
        tool_invocations=[
            {
                "tool": "search_knowledge_base",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "query": "SAIL students",
                    "results": [],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "search_concepts",
            "payload": {"query": "SAIL students"},
        }
    ]


def test_missing_tool_call_retry_forces_explicit_scholarly_materialisation_tool():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="Represent the corresponding paper from #V#uploaded_file_copy_abc123.",
        missing_required_tools=["materialise_scholarly_representation_for_file_copy"],
        missing_required_scholarly_representation_file_copy_ids=[
            "#V#uploaded_file_copy_abc123"
        ],
    )

    assert forced is not None
    assert forced == [
        {
            "action": "call_tool",
            "tool": "materialise_scholarly_representation_for_file_copy",
            "payload": {"concept_id": "#V#uploaded_file_copy_abc123"},
        }
    ]


def test_missing_tool_call_retry_does_not_force_guided_kb_and_web_search() -> None:
    orchestrator = _build_orchestrator_stub()
    prompt = (
        "What open-source projects released recently look most aligned with the "
        "research themes already in my KB?"
    )

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [
            {
                "role": "system",
                "content": (
                    "Selector guidance: first use search_knowledge_base to extract "
                    "research themes from the user's Knowledge Base, then use web "
                    "search to find recent projects."
                ),
            }
        ],
        user_prompt=prompt,
    )

    assert forced is None


def test_missing_tool_call_retry_does_not_force_guided_kb_and_web_search_from_turn_contract():
    orchestrator = _build_orchestrator_stub()
    prompt = (
        "What open-source projects released recently look most aligned with the "
        "research themes already in my KB?"
    )

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        turn_expected_outcome_contract={
            "summary": "Return recent open-source projects grounded against the user's represented research themes.",
            "selector_guidance": (
                "First use search_knowledge_base to extract current research themes "
                "from the KB, then use search_web to identify recent open-source "
                "projects aligned with those themes."
            ),
            "grounding_requirement": (
                "Ground the shortlist in represented themes plus current public web evidence."
            ),
        },
    )

    assert forced is None


def test_missing_tool_call_retry_does_not_force_guided_concept_search_when_contract_names_it():
    orchestrator = _build_orchestrator_stub()
    prompt = (
        "What open-source projects released recently look most aligned with the "
        "research themes already in my KB?"
    )

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        turn_expected_outcome_contract={
            "selector_guidance": (
                "First, use search_knowledge_base and search_concepts to extract "
                "the user's research themes. Second, use search_web to identify "
                "recent open-source releases."
            ),
        },
    )

    assert forced is None


def test_turn_contract_required_tools_include_explicit_task_create():
    required = InternalMCPChatOrchestrator._infer_turn_contract_required_tools(
        turn_expected_outcome_contract={
            "required_tools": ["task_create"],
            "selector_guidance": (
                "Use a task creation workflow (task_create) to represent the diary "
                "entry as a persistent record in Vontology."
            ),
            "summary": "Create a diary entry task for today if one does not exist.",
        },
        method_catalogue=_Gateway.describe_methods(),
    )

    assert "task_create" in required


def test_missing_tool_call_retry_forces_task_create_from_required_tool():
    orchestrator = _build_orchestrator_stub()
    prompt = (
        "If I don't have one, please create me a diary entry for today. It should "
        "record the work I've already done around 6am and 8:30am respectively, "
        "and be open for further work on the rest of today."
    )

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        missing_required_tools=["task_create"],
    )

    assert forced is None


def test_missing_tool_call_retry_does_not_force_jira_search_from_context_prose():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [
            {
                "role": "system",
                "content": "CURRENT USER CONTEXT: Michael Witbrock (#V#michael_witbrock)",
            }
        ],
        user_prompt=(
            "Prepare a short research briefing for me: my represented papers, "
            "relevant recent arXiv work, and any linked Jira tasks."
        ),
        missing_required_tools=["jira_search"],
        user_concept_id="#V#michael_witbrock",
        tool_invocations=[
            {"tool": "search_knowledge_base", "status": "ok"},
            {"tool": "search_concepts", "status": "ok"},
            {"tool": "search_arxiv", "status": "ok"},
        ],
    )

    assert forced is None


def test_missing_tool_call_retry_skips_guided_tools_already_invoked():
    orchestrator = _build_orchestrator_stub()
    prompt = (
        "Prepare a short research briefing for me: my represented papers, "
        "relevant recent arXiv work, and any linked Jira tasks."
    )

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=prompt,
        turn_expected_outcome_contract={
            "selector_guidance": (
                "Use search_knowledge_base, search_concepts, search_web, and "
                "search_arxiv to gather evidence for the briefing."
            ),
            "grounding_requirement": (
                "Papers must be grounded in the KB, recent literature must be "
                "verified via arXiv or web search, and Jira tasks must be "
                "verified via Jira."
            ),
        },
        invoked_tool_names=[
            "search_knowledge_base",
            "search_concepts",
            "search_web",
            "search_arxiv",
        ],
    )

    assert forced is None


def test_run_missing_tool_call_recovery_workflow_projects_turn_contract_from_trace():
    orchestrator = _build_orchestrator_stub()

    class _WorkflowRegistry:
        @staticmethod
        def get(workflow_id: str) -> object | None:
            if workflow_id == MISSING_TOOL_CALL_WORKFLOW_ID:
                return object()
            return None

    captured: dict[str, Any] = {}

    def _execute_workflow(workflow_id: str, **kwargs: Any) -> Any:
        captured["workflow_id"] = workflow_id
        captured["data"] = kwargs.get("data")
        return SimpleNamespace(data={})

    orchestrator._workflow_registry = cast(Any, _WorkflowRegistry())
    orchestrator.execute_workflow = cast(Any, _execute_workflow)
    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda context, max_chars: list(context)
    )
    orchestrator._project_missing_tool_call_aux_telemetry = cast(
        Any, lambda aux_log, recovery_data: None
    )

    request = SimpleNamespace(trace=None)
    environment = SimpleNamespace(
        llm_client=object(),
        user_namespace="#V#test_user",
        auxiliary_system_prompt=None,
        max_tool_invocations=4,
        max_tool_result_chars=8000,
        max_tool_result_field_chars=2000,
        default_gmail_profile=None,
        user_concept_id="#V#test_user",
        org_concept_id="#V#sail",
    )
    turn_contract = {
        "summary": "Return recent open-source projects aligned to the user's represented themes.",
        "selector_guidance": (
            "First use search_knowledge_base to extract the current research themes, "
            "then use search_web to find recent aligned projects."
        ),
        "grounding_requirement": (
            "Ground the answer in represented themes and current web evidence."
        ),
        "answering_guidance": "Return a grounded shortlist rather than a refusal.",
    }

    result = orchestrator._run_missing_tool_call_recovery_workflow(
        request=cast(Any, request),
        environment=cast(Any, environment),
        data={
            "prompt": (
                "What open-source projects released recently look most aligned with "
                "the research themes already in my KB?"
            ),
            "augmented_context": [],
            "selected_workflow_trace": {"expected_outcome_contract": turn_contract},
            "aux_llm_calls": [],
            "model_for_stage": lambda stage: "recovery-model",
            "record_llm_call": None,
            "policy_state": None,
            "prefer_default_model": False,
            "registry_snapshot": {},
            "emit_progress": None,
        },
        response_text="I will look that up.",
        interpretation=None,
        use_structured=True,
        tool_call_parse_error=None,
        tool_calls=None,
        default_model="gemma4:26b",
    )

    assert result == {}
    assert captured["workflow_id"] == MISSING_TOOL_CALL_WORKFLOW_ID
    workflow_context = captured["data"]
    assert isinstance(workflow_context, Mapping)
    assert workflow_context.get("turn_expected_outcome_contract") == turn_contract
    assert (
        workflow_context.get("turn_expected_outcome_summary")
        == turn_contract["summary"]
    )
    assert (
        workflow_context.get("turn_selector_guidance")
        == turn_contract["selector_guidance"]
    )
    assert (
        workflow_context.get("turn_expected_grounding_requirement")
        == turn_contract["grounding_requirement"]
    )
    assert (
        workflow_context.get("turn_answering_guidance")
        == turn_contract["answering_guidance"]
    )


def test_missing_tool_call_retry_injects_retry_context_for_missing_jira_surface():
    orchestrator = _build_orchestrator_stub()
    orchestrator._render_authoritative_prompt = cast(
        Any,
        lambda *args, **kwargs: SimpleNamespace(
            text="Return the missing tool call only.",
            prompt_id="#V#missing_tool_call_retry_prompt",
        ),
    )

    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"jira_search","payload":{"jql":"project = JVNAUTOSCI ORDER BY updated DESC"}}'
        ]
    )

    request = SimpleNamespace(
        data={
            "aux_llm_calls": [],
            "augmented_context": [],
            "user_prompt": (
                "Prepare a short research briefing for me: my represented papers, "
                "relevant recent arXiv work, and any linked Jira tasks."
            ),
            "response_text": "I have your papers and recent arXiv work.",
            "missing_prompt_tools": [],
            "turn_expected_outcome_contract": {
                "summary": (
                    "Return a short research briefing grounded in represented papers, "
                    "recent arXiv work, and linked Jira tasks."
                ),
                "required_tools": [
                    "search_knowledge_base",
                    "search_concepts",
                    "search_arxiv",
                    "jira_search",
                ],
                "selector_guidance": (
                    "Use KB retrieval, arXiv search, and Jira retrieval."
                ),
                "grounding_requirement": (
                    "Jira tasks must be verified via the Jira toolset."
                ),
            },
            "invocations": [
                {"tool": "search_knowledge_base", "status": "ok"},
                {"tool": "search_concepts", "status": "ok"},
                {"tool": "search_web", "status": "ok"},
                {"tool": "search_arxiv", "status": "ok"},
            ],
            "tool_calls": None,
            "tool_call_parse_error": None,
            "record_llm_call": None,
            "policy_state": None,
            "default_model": "gemma4:26b",
            "registry_snapshot": {},
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
            "emit_progress": None,
        },
        environment=SimpleNamespace(
            llm_client=llm,
            model="gemma4:26b",
            user_namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
            auxiliary_system_prompt=None,
        ),
        trace=None,
    )

    result = orchestrator._action_missing_tool_call_retry(cast(Any, request))

    assert result.outputs["missing_tool_call_retry_success"] is True
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "jira_search",
            "payload": {"jql": "project = JVNAUTOSCI ORDER BY updated DESC"},
        }
    ]
    assert llm.calls
    retry_context = llm.calls[0]["context"]
    assert isinstance(retry_context, list) and retry_context
    assert retry_context[0]["role"] == "system"
    assert (
        "jira retrieval step required by the turn contract"
        in retry_context[0]["content"].lower()
    )
    assert (
        "already invoked successfully this turn" in retry_context[0]["content"].lower()
    )


def test_missing_tool_call_retry_action_uses_prior_invocations_for_fetch_follow_up():
    orchestrator = _build_orchestrator_stub()

    request = SimpleNamespace(
        data={
            "aux_llm_calls": [],
            "augmented_context": [],
            "user_prompt": "What predicates are represented for scientific papers?",
            "response_text": "I found matching concepts.",
            "missing_prompt_tools": ["fetch_concept"],
            "invocations": [
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "results": [
                            {
                                "concept_id": "#V#scientific_paper",
                                "name": "scientific paper",
                                "kind": "type",
                            }
                        ],
                    },
                }
            ],
            "tool_calls": None,
            "tool_call_parse_error": None,
            "record_llm_call": None,
            "policy_state": None,
            "default_model": "gemma4:26b",
            "registry_snapshot": {},
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
            "emit_progress": None,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            user_namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
            auxiliary_system_prompt=None,
        ),
        trace=None,
    )

    result = orchestrator._action_missing_tool_call_retry(cast(Any, request))

    assert result.outputs["missing_tool_call_retry_success"] is True
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "fetch_concept",
            "payload": {"concept_id": "#V#scientific_paper"},
        }
    ]
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_forced"
    )


def test_tool_calling_plan_does_not_force_parent_guided_retry_without_explicit_missing_requirements():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any, lambda **kwargs: ("I will search the KB and web now.", "model", None)
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: {}
    )
    orchestrator._store_prompt_requirement_evaluation = cast(
        Any, lambda data, prompt_requirements: None
    )

    class _PromptRequirements:
        required_tools: list[str] = []
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        missing_tools: list[str] = []
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = None

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    request = SimpleNamespace(
        data={
            "prompt": (
                "What open-source projects released recently look most aligned with "
                "the research themes already in my KB?"
            ),
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "emit_phase_transition": None,
            "turn_selector_guidance": (
                "First, use retrieval tools (search_knowledge_base, "
                "get_text_relations_summary) to extract the user's current "
                "research themes. Second, use web search (search_web) to find "
                "recent open-source releases. Third, use an evaluation or "
                "comparison step to match the two."
            ),
            "turn_expected_grounding_requirement": (
                "Alignment must be justified by explicit links between the "
                "properties/themes of the identified projects and the specific "
                "concepts or relations retrieved from the user's KB/Vontology."
            ),
            "turn_expected_outcome_summary": (
                "Identify recent open-source projects that demonstrate high "
                "semantic or topical alignment with the research themes, "
                "entities, and relationships already represented in the user's "
                "authenticated knowledge base."
            ),
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=4,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="respond",
        workflow_state_metadata={},
        action_id="tool_calling.respond",
    )

    result = orchestrator._action_tool_calling_plan(cast(Any, request))

    assert result.outputs["tool_calls_present"] is False
    assert result.outputs["direct_response"] is True
    assert result.outputs["final_response"] == "I will search the KB and web now."
    assert result.outputs["missing_tool_call_recovery_outcome"] is None


def test_tool_calling_backfill_applies_parent_guided_retry_fallback_when_required_surfaces_remain():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda augmented_context, max_chars=4000: list(augmented_context or [])
    )
    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any,
        lambda **kwargs: (
            "**Represented Papers**\\nNo papers found in authenticated context.\\n\\n"
            "**Recent arXiv Work**\\nNo recent arXiv work found in authenticated context.\\n\\n"
            "**Linked Jira Tasks**\\nNo Jira tasks found in authenticated context.",
            "gemma4:26b",
            None,
        ),
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: {}
    )
    orchestrator._store_prompt_requirement_evaluation = cast(
        Any, lambda data, prompt_requirements: None
    )
    orchestrator._augment_prompt_requirements_with_turn_contract = cast(
        Any, lambda **kwargs: kwargs["evaluation"]
    )

    class _PromptRequirements:
        required_tools = [
            "search_knowledge_base",
            "search_concepts",
            "find_relations_with_argument",
            "search_arxiv",
            "jira_search",
        ]
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        missing_tools = ["find_relations_with_argument", "jira_search"]
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = (
            "Required tools still missing after initial retrieval."
        )

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    request = SimpleNamespace(
        data={
            "prompt": (
                "Prepare a short research briefing for me: my represented papers, "
                "relevant recent arXiv work, and any linked Jira tasks."
            ),
            "augmented_context": [
                {
                    "role": "system",
                    "content": (
                        "CURRENT USER CONTEXT: Michael Witbrock (#V#michael_witbrock)"
                    ),
                }
            ],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "iteration_count": 4,
            "remaining_tool_calls": [],
            "invocations": [
                {"tool": "search_knowledge_base", "status": "ok"},
                {"tool": "search_concepts", "status": "ok"},
                {"tool": "search_arxiv", "status": "ok"},
            ],
            "prompt_requirement_url_policy": {},
            "missing_tool_call_retry_reason_override": (
                "Required tools still missing after initial retrieval."
            ),
            "turn_selector_guidance": (
                "Use represented knowledge retrieval, arXiv search, and Jira retrieval."
            ),
            "turn_expected_grounding_requirement": (
                "Papers must be grounded through represented relation evidence, and "
                "Jira tasks must be verified via Jira retrieval."
            ),
            "turn_expected_outcome_summary": (
                "Return a short grounded research briefing covering represented papers, "
                "recent arXiv work, and linked Jira tasks."
            ),
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=8,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="backfill",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(cast(Any, request))

    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    tool_calls = result.outputs["tool_calls"]
    assert tool_calls == [
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {
                "concept_id": "#V#michael_witbrock",
                "limit": 20,
            },
        }
    ]


def test_tool_calling_backfill_chains_ontology_follow_up_after_search_concepts():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda augmented_context, max_chars=4000: list(augmented_context or [])
    )
    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any,
        lambda **kwargs: (
            "I cannot identify grounded predicates yet because only a broad concept search was completed.",
            "gemma4:26b",
            None,
        ),
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: {}
    )
    orchestrator._store_prompt_requirement_evaluation = cast(
        Any, lambda data, prompt_requirements: None
    )
    orchestrator._augment_prompt_requirements_with_turn_contract = cast(
        Any, lambda **kwargs: kwargs["evaluation"]
    )

    class _PromptRequirements:
        required_tools = [
            "search_concepts",
            "get_text_relations_summary",
        ]
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        missing_tools = ["get_text_relations_summary"]
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = (
            "Required ontology follow-up tool was not called after concept search."
        )

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    request = SimpleNamespace(
        data={
            "prompt": "What predicates are salient to SAIL students?",
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "results": [
                            {
                                "concept_id": "#V#sail_student_group",
                                "name": "SAIL Student Group",
                                "relevance_score": 98.0,
                            }
                        ],
                    },
                }
            ],
            "prompt_requirement_url_policy": {},
            "missing_tool_call_retry_reason_override": (
                "Required ontology follow-up tool was not called after concept search."
            ),
            "turn_selector_guidance": (
                "Use search_concepts and get_text_relations_summary before answering."
            ),
            "turn_expected_grounding_requirement": (
                "Predicates must be grounded in represented relationships or text relations."
            ),
            "turn_expected_outcome_summary": (
                "Identify grounded predicates for SAIL students."
            ),
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=6,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="backfill",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(cast(Any, request))

    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#sail_student_group"},
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {
                "concept_id": "#V#sail_student_group",
                "limit": 20,
            },
        },
    ]


def test_tool_calling_backfill_uses_workflow_contract_for_required_text_summary():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda augmented_context, max_chars=4000: list(augmented_context or [])
    )
    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any,
        lambda **kwargs: (
            "I couldn't complete that request because the authoritative conversation-turn workflow did not produce a user-visible response.",
            "gemma4:26b",
            None,
        ),
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: {}
    )
    orchestrator._store_prompt_requirement_evaluation = cast(
        Any, lambda data, prompt_requirements: None
    )
    orchestrator._augment_prompt_requirements_with_turn_contract = cast(
        Any, lambda **kwargs: kwargs["evaluation"]
    )

    class _PromptRequirements:
        required_tools = [
            "fetch_concept",
            "get_text_relations_summary",
            "get_predicate_incidence",
            "find_relations_with_argument",
            "list_uncertain_relationship_assertions",
        ]
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        missing_tools = ["get_text_relations_summary"]
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = (
            "workflow required-effect required tool(s) not yet invoked successfully: "
            "get_text_relations_summary"
        )

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    workflow_required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "grounded_entity_information_retrieval_evidence",
        "required_effects": [
            {
                "effect_id": "grounded_entity_information_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": [
                    "fetch_concept",
                    "get_text_relations_summary",
                    "get_predicate_incidence",
                    "find_relations_with_argument",
                    "list_uncertain_relationship_assertions",
                ],
                "required_tools_match": "all",
                "recovery_strategies": [
                    {
                        "strategy_id": "recover_text_relations_for_focal_entity",
                        "tool": "get_text_relations_summary",
                        "recovers_tools": ["get_text_relations_summary"],
                        "target_concept_source": "required_fetch_or_focal_concept",
                        "target_concept_argument_name": "concept_id",
                    }
                ],
            }
        ],
    }

    request = SimpleNamespace(
        data={
            "prompt": (
                "Tell me about myself as represented here, but separate "
                "established facts from likely inferences."
            ),
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "fetch_concept",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {
                        "success": True,
                        "concept_id": "#V#michael_witbrock",
                    },
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {"success": True},
                },
                {
                    "tool": "find_relations_with_argument",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {"success": True},
                },
                {
                    "tool": "list_uncertain_relationship_assertions",
                    "status": "ok",
                    "effective_arguments": {"source_id": "#V#michael_witbrock"},
                    "effective_payload": {"success": True},
                },
            ],
            "prompt_requirement_url_policy": {},
            "required_prompt_tools": list(_PromptRequirements.required_tools),
            "missing_prompt_tools": ["get_text_relations_summary"],
            "missing_tool_call_retry_reason_override": (
                _PromptRequirements.missing_retry_reason
            ),
            "workflow_required_effects_contract": workflow_required_effects_contract,
            "workflow_required_effects_contract_source": "definition_metadata",
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=6,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="backfill",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(cast(Any, request))

    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#michael_witbrock"},
        }
    ]
    retry_event = next(
        item
        for item in request.data["aux_llm_calls"]
        if item.get("type") == "missing_tool_call_retry"
    )
    assert retry_event["retry_binding_sources"] == [
        {
            "tool": "get_text_relations_summary",
            "binding_source": "workflow_recovery_contract",
            "target_concept_source": "required_fetch_or_focal_concept",
            "recovery_contract_id": (
                "grounded_entity_information_retrieval_evidence"
            ),
            "recovery_contract_source": "definition_metadata",
            "recovery_effect_id": "grounded_entity_information_evidence",
            "recovery_strategy_id": "recover_text_relations_for_focal_entity",
        }
    ]


def test_tool_calling_backfill_uses_workflow_recovery_contract_for_uncertainty_assertions():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda augmented_context, max_chars=4000: list(augmented_context or [])
    )
    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any,
        lambda **kwargs: (
            "I couldn't complete that request because required evidence was missing.",
            "gemma4:26b",
            None,
        ),
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: pytest.fail("represented structural retry should run")
    )
    orchestrator._store_prompt_requirement_evaluation = cast(
        Any, lambda data, prompt_requirements: None
    )
    orchestrator._augment_prompt_requirements_with_turn_contract = cast(
        Any, lambda **kwargs: kwargs["evaluation"]
    )

    class _PromptRequirements:
        required_tools = [
            "fetch_concept",
            "get_text_relations_summary",
            "get_predicate_incidence",
            "find_relations_with_argument",
            "list_uncertain_relationship_assertions",
        ]
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        missing_tools = ["list_uncertain_relationship_assertions"]
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = (
            "workflow required-effect required tool(s) not yet invoked successfully: "
            "list_uncertain_relationship_assertions"
        )

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    workflow_required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "grounded_entity_information_retrieval_evidence",
        "required_effects": [
            {
                "effect_id": "grounded_entity_information_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": list(_PromptRequirements.required_tools),
                "required_tools_match": "all",
                "recovery_strategies": [
                    {
                        "strategy_id": (
                            "recover_uncertain_relationship_assertions_for_focal_entity"
                        ),
                        "tool": "list_uncertain_relationship_assertions",
                        "recovers_tools": [
                            "list_uncertain_relationship_assertions"
                        ],
                        "target_concept_source": "required_fetch_or_focal_concept",
                        "target_concept_argument_name": "source_id",
                        "target_concept_max_count": 2,
                        "default_payload": {"include_legacy": True},
                    }
                ],
            }
        ],
    }

    request = SimpleNamespace(
        data={
            "prompt": (
                "Tell me about my research interests and collaborators as "
                "represented here."
            ),
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "fetch_concept",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {
                        "success": True,
                        "concept_id": "#V#michael_witbrock",
                    },
                },
                {
                    "tool": "get_text_relations_summary",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {"success": True},
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {"success": True},
                },
                {
                    "tool": "find_relations_with_argument",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {"success": True},
                },
            ],
            "prompt_requirement_url_policy": {},
            "required_prompt_tools": list(_PromptRequirements.required_tools),
            "missing_prompt_tools": ["list_uncertain_relationship_assertions"],
            "missing_tool_call_retry_reason_override": (
                _PromptRequirements.missing_retry_reason
            ),
            "workflow_required_effects_contract": workflow_required_effects_contract,
            "workflow_required_effects_contract_source": "definition_metadata",
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=6,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="backfill",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(cast(Any, request))

    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "list_uncertain_relationship_assertions",
            "payload": {
                "include_legacy": True,
                "source_id": "#V#michael_witbrock",
            },
        }
    ]
    retry_event = next(
        item
        for item in request.data["aux_llm_calls"]
        if item.get("type") == "missing_tool_call_retry"
    )
    assert retry_event["retry_binding_sources"] == [
        {
            "tool": "list_uncertain_relationship_assertions",
            "binding_source": "workflow_recovery_contract",
            "target_concept_source": "required_fetch_or_focal_concept",
            "recovery_contract_id": (
                "grounded_entity_information_retrieval_evidence"
            ),
            "recovery_contract_source": "definition_metadata",
            "recovery_effect_id": "grounded_entity_information_evidence",
            "recovery_strategy_id": (
                "recover_uncertain_relationship_assertions_for_focal_entity"
            ),
        }
    ]


def test_tool_calling_backfill_prefers_structural_retry_over_bad_recovery_llm_call():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda augmented_context, max_chars=4000: list(augmented_context or [])
    )
    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any,
        lambda **kwargs: (
            'The ontology inspection identified 194 concepts related to "paper."',
            "gemma4:26b",
            None,
        ),
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any,
        lambda **kwargs: pytest.fail(
            "structural retry should run before the recovery LLM"
        ),
    )
    orchestrator._store_prompt_requirement_evaluation = cast(
        Any, lambda data, prompt_requirements: None
    )
    orchestrator._augment_prompt_requirements_with_turn_contract = cast(
        Any, lambda **kwargs: kwargs["evaluation"]
    )

    class _PromptRequirements:
        required_tools = [
            "vontology_concept_search",
            "get_predicate_incidence",
        ]
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        missing_tools = ["get_predicate_incidence"]
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = (
            "Predicate incidence must target the resolved paper concept."
        )

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    request = SimpleNamespace(
        data={
            "prompt": "What are key predicates for scientific papers in Vontology?",
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "iteration_count": 2,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "vontology_concept_search",
                    "status": "ok",
                    "arguments": {"query": "paper"},
                },
                {
                    "tool": "get_predicate_incidence",
                    "status": "ok",
                    "arguments": {"concept_id": "#V#michael_witbrock"},
                },
            ],
            "prompt_requirement_url_policy": {},
            "missing_tool_call_retry_reason_override": (
                "Predicate incidence must target the resolved paper concept."
            ),
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=8,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="backfill",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(cast(Any, request))

    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "fetch_concept",
            "payload": {"concept_id": "#V#paper"},
        }
    ]


def test_tool_calling_backfill_chains_member_relation_follow_up_from_turn_contract():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda augmented_context, max_chars=4000: list(augmented_context or [])
    )
    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any,
        lambda **kwargs: (
            "I still need member-level relation evidence before answering.",
            "gemma4:26b",
            None,
        ),
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: {}
    )
    orchestrator._augment_prompt_requirements_with_turn_contract = cast(
        Any,
        InternalMCPChatOrchestrator._augment_prompt_requirements_with_turn_contract,
    )

    class _PromptRequirements:
        required_tools: list[str] = []
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        unavailable_required_tools: list[str] = []
        scholarly_representation_intent = False
        missing_tools: list[str] = []
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = None

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    discovery_contract = _build_structured_turn_contract_payload(
        summary=(
            "Identify predicates that demonstrate a verifiable relationship usage "
            "pattern associated with the group or its members."
        ),
        grounding_requirement=(
            "Every listed predicate must be supported by relationship instances or "
            "retrieved relation evidence linking the group or its members."
        ),
        selector_guidance=(
            "Prioritize `search_concepts` to identify the best ontology anchor, then "
            "inspect relationship instances linking the group or its members to "
            "other entities."
        ),
        required_tools=(
            "search_concepts",
            "get_text_relations_summary",
            "find_relations_with_argument",
        ),
    )

    request = SimpleNamespace(
        data={
            "prompt": "What predicates are salient to SAIL students?",
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "results": [
                            {
                                "concept_id": "#V#sail_student_group",
                                "name": "SAIL Student Group",
                                "relevance_score": 98.0,
                            }
                        ],
                    },
                }
            ],
            "prompt_requirement_url_policy": {},
            "workflow_discovery_result": {
                "query": "What predicates are salient to SAIL students?",
                **discovery_contract,
            },
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=6,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="backfill",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(cast(Any, request))

    assert "find_relations_with_argument" in request.data["missing_prompt_tools"]
    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#sail_student_group"},
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {
                "concept_id": "#V#sail_student_group",
                "limit": 20,
            },
        },
    ]


def test_missing_tool_retry_prefers_search_anchor_over_user_anchor_for_ontology_relation_follow_up():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What predicates are salient to SAIL students?",
        missing_required_tools=["find_relations_with_argument"],
        user_concept_id="#V#michael_witbrock",
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#sail_student_group",
                            "name": "SAIL Student Group",
                            "relevance_score": 98.0,
                        }
                    ],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {
                "concept_id": "#V#sail_student_group",
                "limit": 20,
            },
        }
    ]


def test_missing_tool_retry_uses_predicate_incidence_before_filtered_relation_hits_for_explicit_predicate_turn():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What concepts am I in in a #V#author_of relation with?",
        missing_required_tools=[
            "get_predicate_incidence",
            "find_relations_with_argument",
        ],
        user_concept_id="#V#michael_witbrock",
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#author_of",
                            "kind": "predicate",
                            "relevance_score": 99.0,
                        }
                    ],
                },
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "get_predicate_incidence",
            "payload": {
                "concept_id": "#V#michael_witbrock",
                "predicate_filter": ["#V#author_of"],
            },
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {
                "concept_id": "#V#michael_witbrock",
                "predicate_filter": ["#V#author_of"],
                "limit": 20,
            },
        },
    ]


def test_turn_contract_with_member_entity_guidance_requires_relation_argument_retrieval():
    required_tools = InternalMCPChatOrchestrator._infer_turn_contract_required_tools(
        turn_expected_outcome_contract={
            "required_tools": [
                "search_concepts",
                "get_text_relations_summary",
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "summary": (
                "A list of predicates actively used in relationships or properties "
                "associated with entities identified as SAIL students."
            ),
            "grounding_requirement": (
                "Predicates must be explicitly retrieved from the relationship "
                "structures associated with entities identifiable as members of "
                "the SAIL lab."
            ),
            "selector_guidance": (
                "Prefer Vontology retrieval routes that inspect relations of "
                "identified lab members."
            ),
        },
        method_catalogue=_Gateway.describe_methods(),
    )

    assert "search_concepts" in required_tools
    assert "get_text_relations_summary" in required_tools
    assert "get_predicate_incidence" in required_tools
    assert "find_relations_with_argument" in required_tools


def test_turn_contract_expands_concept_search_only_predicate_contract():
    required_tools = InternalMCPChatOrchestrator._infer_turn_contract_required_tools(
        turn_expected_outcome_contract={
            "required_tools": ["vontology_concept_search"],
            "summary": (
                "A structured list of the primary predicates used within Vontology "
                "to characterize scientific papers."
            ),
            "grounding_requirement": (
                "The response must be grounded in actual predicates defined within "
                "the Vontology schema."
            ),
            "selector_guidance": (
                "First resolve the 'scientific paper' concept, then identify "
                "predicates that have an incidence or extent involving that concept."
            ),
        },
        method_catalogue=_Gateway.describe_methods(),
    )

    assert "vontology_concept_search" in required_tools
    assert "get_predicate_incidence" in required_tools


def test_turn_contract_maps_vontology_search_alias_to_allowed_search_concepts():
    required_tools = InternalMCPChatOrchestrator._infer_turn_contract_required_tools(
        turn_expected_outcome_contract={
            "required_tools": ["vontology_concept_search", "fetch_concept"],
            "summary": "List key predicates for scientific papers.",
            "grounding_requirement": (
                "Every predicate listed must be verified against actual "
                "Vontology schema inspection."
            ),
        },
        method_catalogue={
            "search_concepts": {},
            "fetch_concept": {},
            "get_predicate_incidence": {},
        },
        allowed_tools=[
            "search_concepts",
            "fetch_concept",
            "get_predicate_incidence",
        ],
    )

    assert required_tools == (
        "search_concepts",
        "fetch_concept",
        "get_predicate_incidence",
    )


def test_turn_contract_preferring_kb_over_general_web_search_does_not_require_search_web():
    required_tools = InternalMCPChatOrchestrator._infer_turn_contract_required_tools(
        turn_expected_outcome_contract={
            "required_tools": ["search_knowledge_base"],
            "summary": (
                "A precise list of grounded represented records linked to the "
                "authenticated user."
            ),
            "grounding_requirement": (
                "Evidence must come from the authenticated knowledge base or "
                "explicit ontology relations."
            ),
            "selector_guidance": (
                "Prioritize retrieval from the authenticated knowledge base and "
                "relationship-based lookups over general web search."
            ),
        },
        method_catalogue=_Gateway.describe_methods(),
    )

    assert "search_knowledge_base" in required_tools
    assert "search_web" not in required_tools


def test_turn_contract_with_explicit_relation_grounding_language_requires_relation_tools():
    required_tools = InternalMCPChatOrchestrator._infer_turn_contract_required_tools(
        turn_expected_outcome_contract={
            "required_tools": [
                "search_knowledge_base",
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "summary": (
                "A list of represented artefacts explicitly linked to the "
                "authenticated user."
            ),
            "grounding_requirement": (
                "Evidence must explicitly ground the relationship between the "
                "artefact and the user's identity via Vontology relations or "
                "unambiguous metadata attribution."
            ),
            "selector_guidance": (
                "Prioritize retrieval from the knowledge base and ontology when "
                "searching for relations between the user and candidate entities."
            ),
        },
        method_catalogue=_Gateway.describe_methods(),
    )

    assert "search_knowledge_base" in required_tools
    assert "get_predicate_incidence" in required_tools
    assert "find_relations_with_argument" in required_tools


def test_tool_calling_backfill_retries_when_required_ontology_tools_remain_missing():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda augmented_context, max_chars=4000: list(augmented_context or [])
    )
    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any,
        lambda **kwargs: (
            "No predicates or represented relationships for SAIL students were identified.",
            "gemma4:26b",
            None,
        ),
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: {}
    )
    orchestrator._store_prompt_requirement_evaluation = cast(
        Any, lambda data, prompt_requirements: None
    )
    orchestrator._augment_prompt_requirements_with_turn_contract = cast(
        Any, lambda **kwargs: kwargs["evaluation"]
    )

    class _PromptRequirements:
        required_tools = [
            "search_concepts",
            "get_text_relations_summary",
        ]
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        missing_tools = ["get_text_relations_summary"]
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = None

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )
    orchestrator._assess_missing_tool_call = cast(
        Any, lambda **kwargs: SimpleNamespace(retry_reason=None)
    )

    request = SimpleNamespace(
        data={
            "prompt": "What predicates are salient to SAIL students?",
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "results": [
                            {
                                "concept_id": "#V#sail_student_group",
                                "name": "SAIL Student Group",
                                "relevance_score": 98.0,
                            }
                        ],
                    },
                }
            ],
            "prompt_requirement_url_policy": {},
            "turn_selector_guidance": (
                "Use search_concepts and get_text_relations_summary before answering."
            ),
            "turn_expected_grounding_requirement": (
                "Predicates must be grounded in represented relationships or text relations."
            ),
            "turn_expected_outcome_summary": (
                "Identify grounded predicates for SAIL students."
            ),
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=6,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="backfill",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(cast(Any, request))

    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#sail_student_group"},
        },
        {
            "action": "call_tool",
            "tool": "find_relations_with_argument",
            "payload": {"concept_id": "#V#sail_student_group", "limit": 20},
        },
    ]


def test_build_turn_expected_outcome_contract_ignores_discovery_query_guidance_without_structured_state():
    orchestrator = _build_orchestrator_stub()

    discovery_query = (
        "What are key predicates or represented relationships for SAIL students?\n\n"
        "Turn-intent routing guidance:\n"
        "- Routing guidance: Prioritize Vontology tools such as `search_concepts`, "
        "`get_related_concepts`, and `get_text_relations_summary` to identify the "
        "structural role of 'SAIL students' and their associated predicates.\n"
        "- Grounding requirement: All identified predicates or relationships must be "
        "verifiable through Vontology schema inspection (predicates) or retrieved "
        "relation instances (text relations) in the KG.\n"
        "- Required tools: search_concepts, get_text_relations_summary\n"
        "- Success target: A precise list of predicates and relationship types that "
        "explicitly connect 'SAIL students' to other entities or concepts within the "
        "ontology or knowledge base."
    )

    contract = orchestrator._build_turn_expected_outcome_contract(
        {"workflow_discovery_result": {"query": discovery_query}}
    )

    assert contract == {}


def test_build_turn_expected_outcome_contract_uses_structured_discovery_contract_state():
    orchestrator = _build_orchestrator_stub()
    discovery_contract = _build_structured_turn_contract_payload(
        summary=(
            "A precise list of predicates and relationship types that explicitly "
            "connect 'SAIL students' to other entities or concepts within the "
            "ontology or knowledge base."
        ),
        grounding_requirement=(
            "All identified predicates or relationships must be verifiable through "
            "Vontology schema inspection (predicates) or retrieved relation "
            "instances (text relations) in the KG."
        ),
        selector_guidance=(
            "Prioritize Vontology tools such as `search_concepts`, "
            "`get_related_concepts`, and `get_text_relations_summary` to identify "
            "the structural role of 'SAIL students' and their associated predicates."
        ),
        required_tools=("search_concepts", "get_text_relations_summary"),
    )

    contract = orchestrator._build_turn_expected_outcome_contract(
        {"workflow_discovery_result": discovery_contract}
    )

    assert contract == {
        "summary": (
            "A precise list of predicates and relationship types that explicitly "
            "connect 'SAIL students' to other entities or concepts within the "
            "ontology or knowledge base."
        ),
        "grounding_requirement": (
            "All identified predicates or relationships must be verifiable through "
            "Vontology schema inspection (predicates) or retrieved relation "
            "instances (text relations) in the KG."
        ),
        "selector_guidance": (
            "Prioritize Vontology tools such as `search_concepts`, "
            "`get_related_concepts`, and `get_text_relations_summary` to identify "
            "the structural role of 'SAIL students' and their associated predicates."
        ),
    }


def test_build_turn_expected_outcome_contract_object_ignores_prose_required_tools_when_structured_state_present():
    orchestrator = _build_orchestrator_stub()
    structured_contract = _build_structured_turn_contract_payload(
        summary="List grounded represented records linked to the current user.",
        grounding_requirement=(
            "Only surface represented records supported by retrieved evidence."
        ),
        selector_guidance=(
            "Use represented-knowledge retrieval and keep the authenticated actor "
            "context in scope."
        ),
        required_tools=("search_knowledge_base",),
    )

    contract = orchestrator._build_turn_expected_outcome_contract_object(
        {
            **structured_contract,
            "turn_expected_outcome_profile": {
                "summary": "A stale profile summary should not override the contract state.",
                "required_tools": ["jira_search"],
            },
            "augmented_context": [
                {
                    "role": "system",
                    "content": (
                        "Expected answer contract for this turn:\n"
                        "- Required tools: jira_search, search_arxiv\n"
                        "- Success target: Contradictory prose should be ignored."
                    ),
                }
            ],
        }
    )

    assert contract.required_tools == ("search_knowledge_base",)
    assert (
        contract.summary
        == "List grounded represented records linked to the current user."
    )


def test_tool_calling_backfill_recovers_search_concepts_from_structured_discovery_contract():
    orchestrator = _build_orchestrator_stub()

    orchestrator._build_follow_up_llm_context = cast(
        Any, lambda augmented_context, max_chars=4000: list(augmented_context or [])
    )
    orchestrator._build_stage_llm_context = cast(
        Any, lambda **kwargs: (list(kwargs.get("base_context") or []), {})
    )
    orchestrator._run_llm_with_fallbacks = cast(
        Any,
        lambda **kwargs: (
            "No represented relationships or predicates for 'SAIL students' were found.",
            "gemma4:26b",
            None,
        ),
    )
    orchestrator._run_missing_tool_call_recovery_workflow = cast(
        Any, lambda **kwargs: {}
    )

    class _PromptRequirements:
        required_tools: list[str] = []
        required_fetch_concept_ids: list[str] = []
        required_read_file_copy_ids: list[str] = []
        required_scholarly_representation_file_copy_ids: list[str] = []
        required_create_type_name: str | None = None
        required_url_extraction_tool: str | None = None
        required_url_extraction_url: str | None = None
        unavailable_required_tools: list[str] = []
        scholarly_representation_intent = False
        missing_tools: list[str] = []
        missing_fetch_concept_ids: list[str] = []
        missing_read_file_copy_ids: list[str] = []
        missing_scholarly_representation_file_copy_ids: list[str] = []
        missing_retry_reason: str | None = None

    orchestrator._evaluate_prompt_requirements = cast(
        Any, lambda **kwargs: _PromptRequirements()
    )

    discovery_contract = _build_structured_turn_contract_payload(
        summary=(
            "A precise list of predicates and relationship types that explicitly "
            "connect 'SAIL students' to other entities or concepts within the "
            "ontology or knowledge base."
        ),
        grounding_requirement=(
            "All identified predicates or relationships must be verifiable through "
            "Vontology schema inspection (predicates) or retrieved relation "
            "instances (text relations) in the KG."
        ),
        selector_guidance=(
            "Prioritize Vontology tools such as `search_concepts`, "
            "`get_related_concepts`, and `get_text_relations_summary` to identify "
            "the structural role of 'SAIL students' and their associated predicates."
        ),
        required_tools=("search_concepts", "get_text_relations_summary"),
    )

    request = SimpleNamespace(
        data={
            "prompt": (
                "What are key predicates or represented relationships for "
                "SAIL students?"
            ),
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "registry_snapshot": {},
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#sail",
            "model_for_stage": lambda stage: "gemma4:26b",
            "record_llm_call": lambda **kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "emit_progress": None,
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "search_knowledge_base",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "query": "SAIL students",
                        "results": [],
                    },
                }
            ],
            "prompt_requirement_url_policy": {},
            "workflow_discovery_result": {
                "query": (
                    "What are key predicates or represented relationships for "
                    "SAIL students?"
                ),
                **discovery_contract,
            },
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "prefer_default_model": False,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gemma4:26b",
            max_tool_invocations=6,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="backfill",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(cast(Any, request))

    assert "search_concepts" in request.data["missing_prompt_tools"]
    assert "get_text_relations_summary" in request.data["missing_prompt_tools"]
    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "search_concepts",
            "payload": {"query": "SAIL students"},
        }
    ]


def test_missing_tool_call_retry_does_not_force_guided_retrieval_without_guidance():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="What is the capital of France?",
    )

    assert forced is None


def test_missing_tool_call_retry_recovers_search_knowledge_base_from_turn_contract():
    orchestrator = _build_orchestrator_stub()

    forced = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt="List grounded represented records linked to the current user.",
        missing_required_tools=["search_knowledge_base"],
        user_concept_id="#V#test_user",
        tool_invocations=[
            {
                "tool": "search_concepts",
                "payload": {"query": "current user records"},
                "effective_payload": {"query": "current user records"},
            }
        ],
    )

    assert forced == [
        {
            "action": "call_tool",
            "tool": "search_knowledge_base",
            "payload": {
                "query": "current user records",
                "mode": "concepts",
            },
        }
    ]
