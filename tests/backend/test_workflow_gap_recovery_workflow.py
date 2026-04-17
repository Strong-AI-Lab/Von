"""Tests for workflow-gap recovery durable workflows."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.workflow_gap_recovery_workflow import (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
    WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
    WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
    WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
    build_workflow_discovery_gap_recovery_workflow_test_definition,
    build_workflow_gap_test_definition,
    register_workflow_gap_recovery_actions,
)
from src.backend.workflows.workflow_gap_workflow_contracts import (
    WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID,
)
from src.backend.workflows.workflow_template_profile_service import (
    WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID,
)


def test_build_workflow_gap_recovery_definition_shape() -> None:
    workflow = build_workflow_discovery_gap_recovery_workflow_test_definition()

    assert workflow.workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    assert workflow.initial_state == "collect_context"
    assert set(workflow.states.keys()) == {
        "collect_context",
        "analyse_gap",
        "prepare_candidate",
        "create_candidate",
        "decide_test",
        "test_candidate",
        "complete",
        "failed",
    }
    assert workflow.termination_states == ("complete", "failed")

    test_workflow = build_workflow_gap_test_definition()
    assert test_workflow.workflow_id == WORKFLOW_GAP_TEST_WORKFLOW_ID
    assert test_workflow.initial_state == "run_test"
    assert set(test_workflow.states.keys()) == {"run_test", "complete", "failed"}


def test_register_workflow_gap_recovery_actions_registers_expected_ids() -> None:
    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)

    assert {
        "workflow_gap.collect_context",
        "workflow_gap.analyse_recovery",
        "workflow_gap.prepare_candidate_spec",
        WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
        WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
    }.issubset(set(registry.all_action_ids()))


def test_finalise_recovery_asks_user_when_candidate_needs_confirmation(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    monkeypatch.setattr(
        mod,
        "_link_candidate_prompt_to_workflow",
        lambda **_kwargs: None,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)

    result = registry.execute(
        WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
        inputs={},
        context={
            "workflow_gap_analysis_result": {
                "decision": "ask_user",
                "gap_summary": "A reusable workflow for this request is missing.",
                "user_confirmation_prompt": "Should I keep and use this workflow?",
            },
            "workflow_gap_base_response_text": "Fallback reply.",
            "created_candidate_workflow_id": "#V#candidate_gap_workflow",
            "candidate_workflow_name": "Candidate gap workflow",
            "candidate_prompt_concept_id": "#V#candidate_gap_prompt",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert (
        result.outputs["workflow_gap_recovery_outcome"]
        == "candidate_created_awaiting_confirmation"
    )
    assert "Candidate gap workflow (#V#candidate_gap_workflow)" in result.outputs[
        "workflow_gap_final_response_text"
    ]
    assert "Should I keep and use this workflow?" in result.outputs[
        "workflow_gap_final_response_text"
    ]


def test_execute_candidate_disables_recursive_gap_recovery(monkeypatch) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    captured: dict[str, object] = {}

    class _FakeOrchestrator:
        def __init__(
            self,
            *,
            gateway,
            max_tool_invocations,
            default_gmail_profile=None,
        ) -> None:
            captured["max_tool_invocations"] = max_tool_invocations
            captured["gateway"] = gateway
            captured["default_gmail_profile"] = default_gmail_profile

        def run(self, **kwargs):
            captured["run_kwargs"] = dict(kwargs)
            return OrchestratorResult(
                response_text="Retried response.",
                extra_messages=({"role": "tool", "content": "tool output"},),
                tool_invocations=({"tool": "search_concepts"},),
                aux_llm_calls=(),
                llm_calls=(),
            )

    monkeypatch.setattr(
        mod,
        "render_workflow_gap_candidate_prompt",
        lambda **_kwargs: (
            SimpleNamespace(text="Candidate prompt"),
            {"error": None},
        ),
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _FakeOrchestrator,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)
    result = registry.execute(
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        inputs={
            "prompt_concept_id": "#V#candidate_gap_prompt",
            "workflow_gap_dry_run": True,
            "prompt": "Please recover this workflow gap.",
        },
        context={
            "context": [{"role": "user", "content": "Please recover this workflow gap."}],
            "workflow_gap_base_response_text": "Fallback reply.",
        },
        env=WorkflowEnvironment(
            llm_client=object(),
            gateway=SimpleNamespace(enabled=True),
            model="gpt-5.2-chat-latest",
            user_namespace="#V#user@org",
            user_concept_id="#V#user",
            org_concept_id="#V#org",
        ),
    )

    assert result.status == "success"
    assert result.outputs["response_text"] == "Retried response."
    assert captured["max_tool_invocations"] == 0
    run_kwargs = cast(dict[str, Any], captured["run_kwargs"])
    assert run_kwargs["workflow_gap_recovery_enabled"] is False
    assert run_kwargs["prompt"] == "Please recover this workflow gap."
    assert run_kwargs["user_concept_id"] == "#V#user"
    assert run_kwargs["org_concept_id"] == "#V#org"


def test_execute_candidate_uses_canonical_cap_default_when_env_cap_missing(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    captured: dict[str, object] = {}

    class _FakeOrchestrator:
        def __init__(
            self,
            *,
            gateway,
            max_tool_invocations,
            default_gmail_profile=None,
        ) -> None:
            captured["max_tool_invocations"] = max_tool_invocations

        def run(self, **kwargs):
            return OrchestratorResult(
                response_text="Retried response.",
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=(),
                llm_calls=(),
            )

    monkeypatch.setattr(
        mod,
        "render_workflow_gap_candidate_prompt",
        lambda **_kwargs: (
            SimpleNamespace(text="Candidate prompt"),
            {"error": None},
        ),
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _FakeOrchestrator,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)
    result = registry.execute(
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        inputs={
            "prompt_concept_id": "#V#candidate_gap_prompt",
            "workflow_gap_dry_run": False,
            "prompt": "Please recover this workflow gap.",
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=object(),
            gateway=SimpleNamespace(enabled=True),
            model="gpt-5.2-chat-latest",
            max_tool_invocations=None,
        ),
    )

    assert result.status == "success"
    assert (
        captured["max_tool_invocations"]
        == mod.INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT
    )


def test_execute_candidate_recovers_authored_json_guidance_from_step_inputs(
    monkeypatch,
) -> None:
    import json
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    captured: dict[str, object] = {}

    class _FakeOrchestrator:
        def __init__(
            self,
            *,
            gateway,
            max_tool_invocations,
            default_gmail_profile=None,
        ) -> None:
            captured["max_tool_invocations"] = max_tool_invocations
            del gateway, default_gmail_profile

        def run(self, **kwargs):
            captured["run_kwargs"] = dict(kwargs)
            return OrchestratorResult(
                response_text="Grounded response.",
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=(),
                llm_calls=(),
            )

    def _fake_render(**kwargs):
        captured["prompt_variables"] = dict(kwargs["variables"])
        return SimpleNamespace(text="Candidate prompt"), {"error": None}

    monkeypatch.setattr(mod, "render_workflow_gap_candidate_prompt", _fake_render)
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _FakeOrchestrator,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)
    result = registry.execute(
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        inputs={
            "prompt_concept_id": "#V#candidate_gap_prompt",
            "prompt": "Please answer from represented evidence.",
            "workflow_guidance_json": json.dumps(
                [
                    "Resolve the represented concept.",
                    "Surface explicit relation evidence.",
                ]
            ),
            "acceptance_requirements_json": json.dumps(
                [
                    "Ground the answer in represented evidence.",
                    "Name explicit related entities when present.",
                ]
            ),
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=object(),
            gateway=SimpleNamespace(enabled=True),
            model="gpt-5.2-chat-latest",
        ),
    )

    assert result.status == "success"
    prompt_variables = cast(dict[str, Any], captured["prompt_variables"])
    assert json.loads(prompt_variables["workflow_guidance_json"]) == [
        "Resolve the represented concept.",
        "Surface explicit relation evidence.",
    ]
    assert json.loads(prompt_variables["acceptance_requirements_json"]) == [
        "Ground the answer in represented evidence.",
        "Name explicit related entities when present.",
    ]


def test_execute_candidate_prefetches_authenticated_grounding_when_authored(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    captured: dict[str, object] = {}

    class _FakeGateway:
        enabled = True

        def invoke(self, tool_name: str, payload: dict[str, Any]):
            captured["prefetch_tool_name"] = tool_name
            captured["prefetch_payload"] = dict(payload)
            return SimpleNamespace(
                payload={
                    "concept_id": "#V#user",
                    "name": "Michael Witbrock",
                    "relations": {
                        "relations_found": 2,
                        "relations": [
                            {
                                "predicate_id": "hasDescription",
                                "relation_kind": "text",
                                "text_value": {
                                    "text": "Research interests include automated reasoning and context modelling."
                                },
                            },
                            {
                                "predicate_id": "#V#supervises_phd_student",
                                "relation_kind": "binary",
                                "target_previews": {
                                    "#V#timothy_pistotti": {
                                        "name": "Timothy Pistotti"
                                    }
                                },
                            },
                        ],
                    },
                },
                duration_ms=8,
            )

    class _FakeOrchestrator:
        def __init__(
            self,
            *,
            gateway,
            max_tool_invocations,
            default_gmail_profile=None,
        ) -> None:
            captured["max_tool_invocations"] = max_tool_invocations
            del gateway, default_gmail_profile

        def run(self, **kwargs):
            captured["run_kwargs"] = dict(kwargs)
            return OrchestratorResult(
                response_text="Grounded response.",
                extra_messages=(),
                tool_invocations=({"tool": "search_concepts"},),
                aux_llm_calls=(),
                llm_calls=(),
            )

    monkeypatch.setattr(
        mod,
        "render_workflow_gap_candidate_prompt",
        lambda **_kwargs: (
            SimpleNamespace(text="Candidate prompt"),
            {"error": None},
        ),
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _FakeOrchestrator,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)
    result = registry.execute(
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        inputs={
            "prompt_concept_id": "#V#candidate_gap_prompt",
            "prompt": "What do you know about my current research interests, and how do they connect to my collaborators?",
            "workflow_gap_prefetch_profile": "authenticated_concept_relations",
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=object(),
            gateway=_FakeGateway(),
            model="gpt-5.2-chat-latest",
            user_namespace="#V#user@org",
            user_concept_id="#V#user",
        ),
    )

    assert result.status == "success"
    assert captured["prefetch_tool_name"] == "fetch_concept"
    assert captured["max_tool_invocations"] == 0
    run_kwargs = cast(dict[str, Any], captured["run_kwargs"])
    auxiliary_prompt = cast(str, run_kwargs["auxiliary_system_prompt"])
    assert "Authoritative represented evidence pre-fetched for this workflow" in auxiliary_prompt
    assert "Research interests include automated reasoning and context modelling." in auxiliary_prompt
    assert "Timothy Pistotti" in auxiliary_prompt
    tool_invocations = cast(list[dict[str, Any]], result.outputs["tool_invocations"])
    assert tool_invocations[0]["tool"] == "fetch_concept"
    assert tool_invocations[0]["source"] == "workflow_gap_prefetch"
    assert tool_invocations[1]["tool"] == "search_concepts"
    assert result.outputs["workflow_gap_prefetched_grounding_present"] is True
    assert result.outputs["workflow_gap_prefetch_diagnostics"]["profile"] == (
        "authenticated_concept_relations"
    )


def test_execute_candidate_uses_prefetched_authenticated_summary_when_available(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    class _FakeGateway:
        enabled = True

        def invoke(self, tool_name: str, payload: dict[str, Any]):
            del tool_name, payload
            return SimpleNamespace(
                payload={
                    "concept_id": "#V#michael_witbrock",
                    "name": "Michael Witbrock",
                    "relations": {
                        "relations": [
                            {
                                "predicate_id": "hasDescription",
                                "relation_kind": "text",
                                "text_value": {
                                    "text": (
                                        "# Michael Witbrock\n"
                                        "He specialises in automated reasoning, "
                                        "knowledge use, and natural language understanding."
                                    )
                                },
                            },
                            {
                                "predicate_id": "#V#has_paper_matching_profile_json",
                                "relation_kind": "text",
                                "text_value": {
                                    "text": json.dumps(
                                        {
                                            "stated_interest_terms": [
                                                "context modelling",
                                                "latent context variables",
                                                "neuro-symbolic AI",
                                            ]
                                        }
                                    )
                                },
                            },
                            {
                                "predicate_id": "#V#author_of",
                                "relation_kind": "binary",
                                "target_previews": {
                                    "#V#concept_creation_agentic_workflow_design": {
                                        "name": "Concept Creation Agentic Workflow Design",
                                        "kind": "individual",
                                    },
                                    "#V#verified_entity_representation_agentic_workflow_design": {
                                        "name": "Verified Entity Representation Agentic Workflow Design",
                                        "kind": "individual",
                                    },
                                },
                            },
                            {
                                "predicate_id": "#V#member_of_organisation",
                                "relation_kind": "binary",
                                "target_previews": {
                                    "#V#university_of_auckland_strong_ai_lab": {
                                        "name": "University Of Auckland Strong Ai Lab",
                                        "kind": "individual",
                                    }
                                },
                            },
                            {
                                "predicate_id": "#V#supervises_phd_student",
                                "relation_kind": "binary",
                                "target_previews": {
                                    "#V#timothy_pistotti": {
                                        "name": "Timothy Pistotti",
                                        "kind": "individual",
                                    }
                                },
                            },
                            {
                                "predicate_id": "related_to",
                                "relation_kind": "binary",
                                "target_previews": {
                                    "#V#lu_yunli": {
                                        "name": "Lu Yunli",
                                        "kind": "individual",
                                        "concept_id": "#V#lu_yunli",
                                    },
                                    "#V#eugpai_evaluation_proposal_preparation": {
                                        "name": "Eugpai Evaluation Proposal Preparation",
                                        "kind": "individual",
                                        "concept_id": (
                                            "#V#eugpai_evaluation_proposal_preparation"
                                        ),
                                    }
                                },
                            },
                            {
                                "predicate_id": "#V#has_google_scholar_profile",
                                "relation_kind": "binary",
                                "target_previews": {
                                    "#V#google_scholar_profile_for_michael_witbrock": {
                                        "name": "Google Scholar Profile For Michael Witbrock",
                                        "kind": "individual",
                                    }
                                },
                            },
                        ]
                    },
                },
                duration_ms=6,
            )

    class _FakeOrchestrator:
        def __init__(
            self,
            *,
            gateway,
            max_tool_invocations,
            default_gmail_profile=None,
        ) -> None:
            del gateway, max_tool_invocations, default_gmail_profile

        def run(self, **_kwargs):
            return OrchestratorResult(
                response_text="I do not know who you are unless you tell me.",
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=(),
                llm_calls=(),
            )

    monkeypatch.setattr(
        mod,
        "render_workflow_gap_candidate_prompt",
        lambda **_kwargs: (
            SimpleNamespace(text="Candidate prompt"),
            {"error": None},
        ),
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _FakeOrchestrator,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)
    result = registry.execute(
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        inputs={
            "prompt_concept_id": "#V#candidate_gap_prompt",
            "prompt": "What do you know about my current research interests, and how do they connect to my collaborators?",
            "workflow_gap_prefetch_profile": "authenticated_concept_relations",
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=object(),
            gateway=_FakeGateway(),
            model="gpt-5.2-chat-latest",
            user_namespace="#V#michael_witbrock@org",
            user_concept_id="#V#michael_witbrock",
        ),
    )

    assert result.status == "success"
    response_text = cast(str, result.outputs["response_text"])
    assert "Represented research-interest evidence for Michael Witbrock includes" in response_text
    assert "context modelling" in response_text
    assert "Profile description:" in response_text
    assert "Concept Creation Agentic Workflow Design" in response_text
    assert "University Of Auckland Strong Ai Lab" in response_text
    assert "Timothy Pistotti (supervises PhD student)" in response_text
    assert "Lu Yunli (related to)" in response_text
    assert "Eugpai Evaluation Proposal Preparation" not in response_text
    assert "I do not currently have" not in response_text
    assert result.outputs["workflow_gap_prefetched_response_used"] is True


def test_prepare_candidate_spec_uses_authored_gap_candidate_template(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    monkeypatch.setattr(
        mod,
        "render_workflow_gap_candidate_prompt",
        lambda **_kwargs: (
            SimpleNamespace(text="Candidate prompt"),
            {"error": None},
        ),
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)
    result = registry.execute(
        WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID,
        inputs={},
        context={
            "prompt": "Recover this missing workflow.",
            "workflow_gap_request_text": "Recover this missing workflow.",
            "workflow_gap_should_create_candidate": True,
            "workflow_gap_base_response_text": "Fallback reply.",
            "workflow_gap_recent_turns_json": "[]",
            "workflow_gap_analysis_result": {
                "workflow_name": "Candidate Gap Workflow",
                "workflow_description": "Recover this missing workflow.",
                "gap_summary": "No suitable workflow matched.",
                "intent_summary": "workflow gap recovery",
                "workflow_guidance": ["Prefer reusable workflow surfaces."],
                "acceptance_requirements": ["Produce a better routed response."],
            },
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    template_resolution = result.outputs["workflow_gap_candidate_template_resolution"]
    assert template_resolution["template_id"] == (
        WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID
    )
    candidate_spec = result.outputs["candidate_workflow_spec"]
    assert candidate_spec["steps"][0]["action_id"] == "workflow_gap.execute_candidate"
    assert candidate_spec["steps"][0]["inputs"]["prompt_concept_id"] == (
        result.outputs["candidate_prompt_concept_id"]
    )
    assert "default_request_text" not in candidate_spec["steps"][0]["inputs"]
    assert "default_recent_turns_json" not in candidate_spec["steps"][0]["inputs"]
    assert "default_base_response_text" not in candidate_spec["steps"][0]["inputs"]
    assert result.outputs["candidate_prompt_concept_id"] == (
        mod.WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID
    )


def test_workflow_gap_recovery_workflows_publish_authoritatively(monkeypatch) -> None:
    from src.backend.db.mongo_client import get_db
    from src.backend.services.workflow_discovery_service import (
        invalidate_workflow_discovery_executability_caches,
    )
    from src.backend.workflows import (
        workflow_concept_authority_service as authority_service,
    )
    from workflow_test_support import (
        bootstrap_authoritative_reasoning_recovery_workflows,
    )

    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    authority_service.clear_workflow_type_resolution_cache()

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    invalidate_workflow_discovery_executability_caches()
    report = bootstrap_authoritative_reasoning_recovery_workflows()
    graph_publication = report.get("graph_publication") or {}
    published_ids = set(graph_publication.get("published_workflow_ids") or [])
    errors_by_workflow_id = graph_publication.get("errors_by_workflow_id") or {}

    assert WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID in published_ids
    assert WORKFLOW_GAP_TEST_WORKFLOW_ID in published_ids
    assert WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID not in errors_by_workflow_id
    assert WORKFLOW_GAP_TEST_WORKFLOW_ID not in errors_by_workflow_id
