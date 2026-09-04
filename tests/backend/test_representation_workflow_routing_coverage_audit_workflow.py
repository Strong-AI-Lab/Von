from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.representation_workflow_routing_coverage_audit_contracts import (
    REPRESENTATION_ROUTING_AUDIT_BUILD_EPISODE_EVIDENCE_ACTION_ID,
    REPRESENTATION_ROUTING_AUDIT_COLLECT_EVIDENCE_ACTION_ID,
    REPRESENTATION_ROUTING_AUDIT_FINALISE_ACTION_ID,
    REPRESENTATION_ROUTING_AUDIT_LOAD_PROFILE_ACTION_ID,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_LINK_PREDICATE,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_PREDICATE,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_LINK_PREDICATE,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
)
from src.backend.services.representation_workflow_routing_coverage_audit_vontology_service import (
    bootstrap_canonical_representation_workflow_routing_coverage_audit_workflow,
    load_representation_workflow_routing_coverage_audit_profile,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable import (
    representation_workflow_routing_coverage_audit_workflow as audit_workflow,
)
from src.backend.workflows.durable.registry_factory import build_durable_action_registry
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db
    from src.backend.workflows import (
        workflow_concept_authority_service as authority_service,
    )

    db = get_db()
    if db is not None:
        for collection_name in (
            "concepts",
            "text_relations",
            "text_values",
            "workflow_event_bindings",
            "workflow_instances",
        ):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    authority_service.clear_workflow_type_resolution_cache()
    yield
    authority_service.clear_workflow_type_resolution_cache()


def _profile() -> dict[str, Any]:
    return {
        "schema_version": "representation_workflow_routing_coverage_audit_profile.v1",
        "profile_id": "test_profile",
        "profile_concept_id": "#V#test_profile",
        "audit_policy": {
            "max_cases": 2,
            "max_discovery_results": 4,
            "discovery_relevance_threshold": 0.05,
            "discovery_timeout_seconds": 1.0,
            "minimum_expected_presence_rate": 0.8,
            "minimum_expected_top_rank_rate": 0.5,
        },
        "inventory_policy": {
            "mode": "expected_workflows",
            "max_workflows": 10,
            "workflow_ids": [],
        },
        "evaluation_dimensions": [],
        "suggestion_policy": {
            "python_generated_improvement_suggestions_allowed": False,
        },
        "audit_cases": [
            {
                "case_id": "article_metadata",
                "task_class": "scholarly_article_representation",
                "query": "Represent this paper from title, authors, DOI, and abstract.",
                "expected_workflow_ids": [
                    "#V#scholarly_article_metadata_representation_workflow"
                ],
                "priority": "high",
            }
        ],
    }


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    audit_workflow.register_representation_workflow_routing_coverage_audit_actions(
        registry
    )
    return registry


def _environment() -> WorkflowEnvironment:
    return WorkflowEnvironment(llm_client=None, user_namespace="test")


def _find_nested_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_find_nested_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_find_nested_key(item, key) for item in value)
    return False


def _state_with_suffix(definition: Any, suffix: str) -> Any:
    return next(
        (
            state
            for state_id, state in definition.states.items()
            if str(state_id).endswith(f"_{suffix}")
        ),
        None,
    )


def test_workflow_structure_uses_llm_and_episode_persist() -> None:
    definition = (
        audit_workflow.build_representation_workflow_routing_coverage_audit_test_definition()
    )

    assert (
        definition.workflow_id
        == REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID
    )
    assert definition.initial_state == "load_profile"
    assert definition.states["evaluate_evidence"].actions[0].action_id == "llm.action"
    assert definition.states["evaluate_evidence"].actions[0].is_llm_step
    assert definition.states["evaluate_evidence"].actions[0].prompt_contract == {
        "requested_prompt_concept_ids": [
            REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
        ]
    }
    assert (
        definition.states["persist_episode_memory"].actions[0].action_id
        == "episode_critic.persist_memory"
    )
    assert (
        definition.states["evaluate_evidence"].metadata["tool_output_context_mappings"][
            0
        ]["context_key"]
        == "critic_assessment"
    )


def test_collect_evidence_does_not_generate_improvement_suggestions(
    monkeypatch,
) -> None:
    class _FakeDiscoveryResult:
        def to_dict(self) -> dict[str, Any]:
            match = {
                "concept_id": "#V#scholarly_article_metadata_representation_workflow",
                "name": "Scholarly Article Metadata Representation Workflow",
                "relevance_score": 0.98,
                "routing_eligible": True,
                "is_executable": True,
            }
            return {
                "requested_query": "Represent this paper",
                "candidates": [match],
                "routing_matches": [match],
                "match_absence_reason": None,
                "errors": None,
            }

    monkeypatch.setattr(
        audit_workflow,
        "discover_workflows",
        lambda *_args, **_kwargs: _FakeDiscoveryResult(),
    )
    monkeypatch.setattr(
        audit_workflow,
        "_workflow_inventory_row",
        lambda workflow_id: {
            "workflow_id": workflow_id,
            "concept_exists": True,
            "graph_present": True,
        },
    )

    context = {"representation_routing_audit_profile": _profile()}
    result = _registry().execute(
        REPRESENTATION_ROUTING_AUDIT_COLLECT_EVIDENCE_ACTION_ID,
        inputs={},
        context=context,
        env=_environment(),
    )

    assert result.status == "success"
    report = result.outputs["representation_routing_audit_report"]
    assert report["suggestion_authority"] == "workflow_llm_step"
    assert report["python_authored_improvement_suggestions"] is False
    assert report["metrics"]["objective_status"] == "passes_profile_thresholds"
    assert not _find_nested_key(report, "improvement_suggestions")


def test_build_episode_evidence_passes_through_workflow_authored_assessment() -> None:
    critic_assessment = {
        "verdict": "needs_improvement",
        "confidence": 0.86,
        "summary": "Expected workflow was absent from routing matches.",
        "maintenance_follow_up_recommended": True,
        "maintenance_follow_up_reason": "workflow_routing_coverage_gap",
        "recommendations": ["Repair workflow routing metadata."],
        "root_causes": [],
        "improvement_suggestions": [
            {
                "suggestion_id": "repair_article_representation_routing",
                "category": "workflow_change",
                "priority": "high",
                "target_surface": "workflow",
                "target_workflow_id": (
                    "#V#scholarly_article_metadata_representation_workflow"
                ),
                "title": "Broaden article representation discovery",
                "rationale": "The general metadata task class failed discovery.",
                "suggested_change": "Add source-agnostic discovery exemplars.",
                "evidence_refs": ["case:article_metadata"],
                "recursion_level": 0,
            }
        ],
    }
    report = {
        "schema_version": "representation_workflow_routing_coverage_audit_report.v1",
        "audit_run_id": "audit-1",
        "metrics": {"objective_status": "below_profile_thresholds"},
    }
    context = {
        "representation_routing_audit_report": report,
        "representation_routing_audit_profile": _profile(),
        "critic_assessment": critic_assessment,
        "request_id": "request-1",
    }
    result = _registry().execute(
        REPRESENTATION_ROUTING_AUDIT_BUILD_EPISODE_EVIDENCE_ACTION_ID,
        inputs={},
        context=context,
        env=_environment(),
    )

    assert result.status == "success"
    assert result.outputs["critic_assessment"] == critic_assessment
    bundle = result.outputs["episode_evidence_bundle"]
    assert bundle["episode_locator"]["request_id"] == "request-1"
    assert bundle["observed_evidence"]["representation_routing_audit_report"] == report
    assert (
        bundle["expected_context"]["python_authored_improvement_suggestions"] is False
    )


def test_finalise_copies_persisted_suggestions_without_authoring_them() -> None:
    suggestions = [{"suggestion_id": "workflow_authored"}]
    context = {
        "representation_routing_audit_report": {
            "audit_run_id": "audit-2",
            "metrics": {"objective_status": "below_profile_thresholds"},
        },
        "critic_assessment": {"summary": "Workflow authored this."},
        "episode_critique_memory_id": "#V#episode_memory",
        "improvement_suggestions": suggestions,
        "self_improvement": {"launches": []},
    }
    result = _registry().execute(
        REPRESENTATION_ROUTING_AUDIT_FINALISE_ACTION_ID,
        inputs={},
        context=context,
        env=_environment(),
    )

    assert result.status == "success"
    audit_result = result.outputs["representation_routing_audit_result"]
    assert audit_result["improvement_suggestions"] == suggestions
    assert audit_result["critic_assessment"] == {"summary": "Workflow authored this."}


def test_durable_action_registry_registers_audit_support_actions() -> None:
    registry = build_durable_action_registry()

    assert registry.has(REPRESENTATION_ROUTING_AUDIT_LOAD_PROFILE_ACTION_ID)
    assert registry.has(REPRESENTATION_ROUTING_AUDIT_COLLECT_EVIDENCE_ACTION_ID)
    assert registry.has(REPRESENTATION_ROUTING_AUDIT_BUILD_EPISODE_EVIDENCE_ACTION_ID)
    assert registry.has(REPRESENTATION_ROUTING_AUDIT_FINALISE_ACTION_ID)


def test_bootstrap_materialises_represented_audit_workflow_authority(
    _reset_mock_db: Any,
) -> None:
    report = (
        bootstrap_canonical_representation_workflow_routing_coverage_audit_workflow()
    )

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert report.get("success") is True
    assert counts.get("errors") == 0
    assert counts.get("workflows_published") == 1

    definition = load_workflow_definition_from_vontology(
        REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID
    )
    assert definition is not None
    action_ids = [
        action.action_id
        for state in definition.states.values()
        for action in state.actions
        if action.action_id
    ]
    assert action_ids == [
        REPRESENTATION_ROUTING_AUDIT_LOAD_PROFILE_ACTION_ID,
        REPRESENTATION_ROUTING_AUDIT_COLLECT_EVIDENCE_ACTION_ID,
        "llm.action",
        REPRESENTATION_ROUTING_AUDIT_BUILD_EPISODE_EVIDENCE_ACTION_ID,
        "episode_critic.persist_memory",
        REPRESENTATION_ROUTING_AUDIT_FINALISE_ACTION_ID,
    ]

    evaluate_state = _state_with_suffix(definition, "evaluate_evidence")
    assert evaluate_state is not None
    llm_action = evaluate_state.actions[0]
    assert (llm_action.prompt_contract or {}).get("requested_prompt_concept_ids") == [
        REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
    ]
    llm_context_keys = {
        item.get("context_key")
        for item in (llm_action.llm_policy or {}).get("context_fields") or []
    }
    assert "representation_routing_audit_profile" in llm_context_keys
    assert "representation_routing_audit_report" in llm_context_keys
    assert {
        "workflow_success_guidance_history",
        "workflow_failure_avoidance_history",
        "workflow_low_imposition_exploration_history",
    }.isdisjoint(llm_context_keys)

    prompt_links = get_texts_for_concept(
        REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
        predicate=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_LINK_PREDICATE,
        limit=5,
    )
    assert any(
        (row or {}).get("text")
        == REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
        for row in prompt_links
    )
    prompt_rows = get_texts_for_concept(
        REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in prompt_rows if (row or {}).get("text")),
        "",
    )
    assert "must not author repair suggestions" in prompt_text
    assert "Do not suggest Python branch tables" in prompt_text

    profile_links = get_texts_for_concept(
        REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
        predicate=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_LINK_PREDICATE,
        limit=5,
    )
    assert any(
        (row or {}).get("text")
        == REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID
        for row in profile_links
    )
    profile_rows = get_texts_for_concept(
        REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_CONCEPT_ID,
        predicate=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROFILE_PREDICATE,
        limit=5,
    )
    assert any((row or {}).get("text") for row in profile_rows)

    profile, diagnostics = load_representation_workflow_routing_coverage_audit_profile(
        workflow_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID
    )
    assert diagnostics.get("error_code") is None
    assert profile is not None
    assert len(profile.get("audit_cases") or []) >= 8
    assert (
        profile.get("suggestion_policy", {}).get(
            "python_generated_improvement_suggestions_allowed"
        )
        is False
    )
