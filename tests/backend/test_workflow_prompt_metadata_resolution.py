from __future__ import annotations

from unittest.mock import patch

from src.backend.services import prompt_template_service as pts
from src.backend.workflows import prompt_metadata_resolution as pmr


def _install_prompt_resolution_fixtures(*, texts: dict, docs: dict):
    def _fake_find_one(query, projection=None):
        concept_id = query.get("concept_id") if isinstance(query, dict) else None
        if not isinstance(concept_id, str):
            return None
        if concept_id not in docs:
            return None
        if projection == {"_id": 1}:
            return {"_id": concept_id}
        return docs[concept_id]

    return (
        patch.object(
            pmr.ConceptsRepository,
            "find_one",
            side_effect=_fake_find_one,
        ),
        patch.object(
            pmr,
            "get_texts_for_concept",
            side_effect=lambda concept_id, *args, **kwargs: texts.get(concept_id, []),
        ),
        patch.object(
            pts,
            "get_texts_for_concept",
            side_effect=lambda concept_id, *args, **kwargs: texts.get(concept_id, []),
        ),
    )


def test_resolve_workflow_prompt_metadata_merges_prompt_agent_and_defaults():
    texts = {
        "#V#issue_prompt": [
            {"predicate": "#V#hasContent", "text": "Fix issue {issue_key}"},
            {"predicate": "#V#hasPromptName", "text": "Issue fixer"},
            {"predicate": "#V#usesModelPreference", "text": "gpt-5"},
            {
                "predicate": "#V#hasToolResolutionPriority",
                "text": "github_create_pull_request_with_copilot,jira_add_comment",
            },
        ],
        "#V#default_agent_profile": [
            {"predicate": "#V#hasPromptDescription", "text": "Agent-supplied description"},
            {"predicate": "#V#hasPromptScope", "text": "organisation"},
        ],
    }
    docs = {
        "#V#issue_prompt": {
            "concept_id": "#V#issue_prompt",
            "relationships": {
                "#V#usesAgentProfile": ["#V#default_agent_profile"],
                "#V#allowsTool": [
                    "jira_add_comment",
                    "github_create_pull_request_with_copilot",
                ],
            },
        },
        "#V#default_agent_profile": {
            "concept_id": "#V#default_agent_profile",
            "relationships": {
                "#V#allowsTool": ["jira_add_comment"],
                "#V#usesModelPreference": ["gpt-4.1"],
            },
        },
    }

    patches = _install_prompt_resolution_fixtures(texts=texts, docs=docs)
    with patches[0], patches[1], patches[2]:
        resolution = pmr.resolve_workflow_prompt_metadata(
            prompt_concept_ids=["#V#issue_prompt"],
            defaults={
                "allowed_tools": ["fallback_tool"],
                "prompt_source": "workflow_default",
            },
            available_tools=[
                "jira_add_comment",
                "github_create_pull_request_with_copilot",
            ],
        )
        contract = pmr.build_workflow_prompt_contract(
            resolution=resolution,
            validation_policy=pmr.PROMPT_VALIDATION_POLICY_FAIL,
        )

    assert resolution.resolved_prompt_concept_id == "#V#issue_prompt"
    assert resolution.metadata["prompt_name"] == "Issue fixer"
    assert resolution.metadata["prompt_description"] == "Agent-supplied description"
    assert resolution.metadata["model_preference"] == "gpt-5"
    assert resolution.metadata["prompt_scope"] == "organisation"
    assert resolution.metadata["prompt_source"] == "workflow_default"
    assert resolution.metadata["prompt_variables"] == ["issue_key"]
    assert resolution.metadata["allowed_tools"] == [
        "github_create_pull_request_with_copilot",
        "jira_add_comment",
    ]
    assert resolution.diagnostics["status"] == "ok"
    assert resolution.diagnostics["errors"] == []
    assert contract["validation_policy"] == "fail"
    assert contract["requested_prompt_concept_ids"] == ["#V#issue_prompt"]
    assert contract["resolved_prompt_concept_id"] == "#V#issue_prompt"


def test_resolve_workflow_prompt_metadata_warns_for_unavailable_tools():
    texts = {
        "#V#limited_prompt": [
            {"predicate": "#V#hasContent", "text": "Summarise {issue_key}"}
        ]
    }
    docs = {
        "#V#limited_prompt": {
            "concept_id": "#V#limited_prompt",
            "relationships": {
                "#V#allowsTool": ["missing.tool"],
            },
        }
    }

    patches = _install_prompt_resolution_fixtures(texts=texts, docs=docs)
    with patches[0], patches[1], patches[2]:
        resolution = pmr.resolve_workflow_prompt_metadata(
            prompt_concept_ids=["#V#limited_prompt"],
            validation_policy=pmr.PROMPT_VALIDATION_POLICY_WARN,
            available_tools=["jira_search"],
        )

    assert resolution.diagnostics["status"] == "warning"
    assert resolution.diagnostics["errors"] == []
    assert resolution.diagnostics["warnings"] == [
        "prompt_allowed_tools_unavailable:missing.tool"
    ]


def test_resolve_model_prompt_variant_prefers_exact_model_then_base_fallback():
    texts = {
        "#V#base_prompt": [
            {"predicate": "#V#hasContent", "text": "Base prompt for {task}"},
        ],
        "#V#family_prompt": [
            {"predicate": "#V#hasContent", "text": "Family prompt for {task}"},
            {"predicate": "#V#forModelFamily", "text": "ollama"},
        ],
        "#V#exact_prompt": [
            {"predicate": "#V#hasContent", "text": "Exact prompt for {task}"},
            {"predicate": "#V#forModel", "text": "ollama:local-reasoner:7b"},
        ],
    }
    docs = {
        "#V#base_prompt": {
            "concept_id": "#V#base_prompt",
            "relationships": {
                "#V#hasModelPromptVariant": [
                    "#V#family_prompt",
                    "#V#exact_prompt",
                ],
            },
        },
        "#V#family_prompt": {
            "concept_id": "#V#family_prompt",
            "relationships": {},
        },
        "#V#exact_prompt": {
            "concept_id": "#V#exact_prompt",
            "relationships": {},
        },
    }

    patches = _install_prompt_resolution_fixtures(texts=texts, docs=docs)
    with patches[0], patches[1], patches[2]:
        resolution = pmr.resolve_workflow_prompt_metadata(
            prompt_concept_ids=["#V#base_prompt"],
            available_tools=[],
        )
        exact = pmr.resolve_model_prompt_variant(
            base_prompt_concept_id="#V#base_prompt",
            base_prompt_text=resolution.prompt_text,
            variables={"task": "tool use"},
            selected_model="ollama:local-reasoner:7b",
            selected_candidate={"provider": "ollama", "locality": "local"},
        )
        fallback = pmr.resolve_model_prompt_variant(
            base_prompt_concept_id="#V#base_prompt",
            base_prompt_text="Base prompt for tool use",
            variables={"task": "tool use"},
            selected_model="openai:gpt-test",
            selected_candidate={"provider": "openai", "locality": "external"},
        )

    assert resolution.metadata["model_prompt_variant_ids"] == [
        "#V#family_prompt",
        "#V#exact_prompt",
    ]
    assert exact.selected_prompt_concept_id == "#V#exact_prompt"
    assert exact.prompt_text == "Exact prompt for tool use"
    assert exact.match_reason == "exact_model"
    assert exact.diagnostics["base_prompt_concept_id"] == "#V#base_prompt"
    assert exact.diagnostics["match_reason"] == "exact_model"
    assert fallback.selected_prompt_concept_id == "#V#base_prompt"
    assert fallback.prompt_text == "Base prompt for tool use"
    assert fallback.diagnostics["match_reason"] == "base_prompt"
    assert fallback.diagnostics["fallback_reason"] == "no_matching_variant"
