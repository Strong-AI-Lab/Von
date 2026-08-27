from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from src.backend.services import workflow_repo_seed_bootstrap as seed_bootstrap
from src.backend.services.paper_recommendation_constants import (
    GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
    PAPER_MATCHING_PROFILE_MAINTENANCE_PROMPT_CONCEPT_ID,
    PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID,
    PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_REQUESTED_EVENT_TYPE,
    PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_WORKFLOW_ID,
)
from src.backend.services.paper_recommendation_workflow_vontology_service import (
    _REPO_SEED_ASSET_PATH,
    _ensure_paper_recommendation_prompt_support,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_event_integration_service import (
    EVENT_TYPE_RELATIONSHIP_ADDED,
    EVENT_TYPE_RELATIONSHIP_REMOVED,
    EVENT_TYPE_SCOPED_ASSERTION_RETRACTED,
    EVENT_TYPE_SCOPED_ASSERTION_UPSERTED,
    EVENT_TYPE_TEXT_RELATION_UPDATED,
    EVENT_TYPE_TEXT_RELATION_UPSERTED,
)
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in (
            "concepts",
            "text_relations",
            "text_values",
            "workflow_event_bindings",
        ):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    yield
    authority_service.clear_workflow_type_resolution_cache()


def test_paper_recommendation_prompt_support_seeds_content_from_repo_asset(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_paper_recommendation_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 4

    prompt_rows = get_texts_for_concept(
        PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in prompt_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(prompt_text, str)
    assert "Prefer semantically relevant matches across languages" in prompt_text
    assert "Use the embedding_score only as one signal" in prompt_text

    delivery_rows = get_texts_for_concept(
        PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    delivery_text = next(
        ((row or {}).get("text") for row in delivery_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(delivery_text, str)
    assert "Von has {recommendation_count} new {recommendation_noun} for you." in delivery_text
    assert "{recommendation_items}" in delivery_text

    rationale_rows = get_texts_for_concept(
        PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    rationale_text = next(
        ((row or {}).get("text") for row in rationale_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(rationale_text, str)
    assert "Do not mention embeddings, ranking pipelines, or internal selection machinery." in rationale_text
    assert "rationale_summary: one paragraph suitable for direct user display" in rationale_text

    maintenance_rows = get_texts_for_concept(
        PAPER_MATCHING_PROFILE_MAINTENANCE_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    maintenance_text = next(
        ((row or {}).get("text") for row in maintenance_rows if (row or {}).get("text")),
        "",
    )
    normalised_maintenance_text = " ".join(maintenance_text.split())
    assert "Preserve explicit preferences" in maintenance_text
    assert "arbitrary latest profile" in normalised_maintenance_text
    assert "actor-effective `#V#has_paper_matching_profile_json` relations with" in (
        maintenance_text
    )
    assert "optional `source_context`, it must be a JSON object" in (
        maintenance_text
    )
    assert "omit it instead of sending a string" in normalised_maintenance_text
    assert "use `user_only_default` for output scope" in normalised_maintenance_text
    assert "Never pass `user` or `organisation` as `selected_scope_mode`" in (
        normalised_maintenance_text
    )
    assert "call `get_text_relations` for that exact source artefact without a predicate filter" in (
        normalised_maintenance_text
    )
    assert "Do not assume the canonical base predicate is written with a `#V#` prefix" in (
        normalised_maintenance_text
    )

    bundle = json.loads(_REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    profile_workflow = next(
        workflow
        for workflow in bundle["workflows"]
        if workflow["workflow_id"] == PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID
    )
    maintenance_step = next(
        step
        for step in profile_workflow["publication_spec"]["steps"]
        if step["state_id"] == "plan_profile_maintenance"
    )
    text_relation_defaults = maintenance_step["llm_policy"]["tool_argument_defaults"][
        "get_text_relations"
    ]
    assert text_relation_defaults == {"limit": 8}


def test_forced_profile_prompt_seed_refreshes_existing_content(
    _reset_mock_db: Any,
) -> None:
    _ensure_paper_recommendation_prompt_support()
    from src.backend.services.text_value_service import upsert_singleton_text_relation

    upsert_singleton_text_relation(
        subject_concept_id=PAPER_MATCHING_PROFILE_MAINTENANCE_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        text="stale profile-maintenance prompt",
        lang="en-NZ",
        garbage_collect=True,
    )

    report = _ensure_paper_recommendation_prompt_support(force_prompt_seed=True)

    assert report.get("success") is True
    assert PAPER_MATCHING_PROFILE_MAINTENANCE_PROMPT_CONCEPT_ID in report.get(
        "seeded_prompt_ids", []
    )
    maintenance_rows = get_texts_for_concept(
        PAPER_MATCHING_PROFILE_MAINTENANCE_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    maintenance_text = next(
        ((row or {}).get("text") for row in maintenance_rows if (row or {}).get("text")),
        "",
    )
    assert maintenance_text != "stale profile-maintenance prompt"
    assert "optional `source_context`, it must be a JSON object" in maintenance_text


def test_paper_recommendation_event_bindings_are_conditioned_by_policy(
    _reset_mock_db: Any,
) -> None:
    bundle = authority_service.load_repo_seed_workflow_bundle(_REPO_SEED_ASSET_PATH)
    represented = bundle["workflow_event_bindings"]
    bindings = {
        item["event_type"]: item
        for item in represented[PAPER_RECOMMENDATION_WORKFLOW_ID]
    }

    assert bindings[PAPER_RECOMMENDATION_REQUESTED_EVENT_TYPE].get("condition") is None

    text_condition = bindings[EVENT_TYPE_TEXT_RELATION_UPSERTED]["condition"]
    assert text_condition == {
        "kind": "context_value_in",
        "key": "event.predicate",
        "values": [
            GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
            "#V#has_paper_recommendation_profile_json",
        ],
    }
    assert bindings[EVENT_TYPE_TEXT_RELATION_UPDATED]["condition"] == text_condition

    relationship_condition = bindings[EVENT_TYPE_RELATIONSHIP_ADDED]["condition"]
    assert relationship_condition == {
        "kind": "context_value_in",
        "key": "event.predicate",
        "values": [
            "#V#has_research_interest",
            "#V#member_of_organisation",
            "#V#has_project",
            "#V#working_on_project",
        ],
    }
    assert bindings[EVENT_TYPE_RELATIONSHIP_REMOVED]["condition"] == relationship_condition
    assert bindings[EVENT_TYPE_SCOPED_ASSERTION_UPSERTED]["condition"] == {
        "kind": "context_value_equals",
        "key": "event.predicate",
        "value": GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
    }
    assert bindings[EVENT_TYPE_SCOPED_ASSERTION_RETRACTED]["condition"] == bindings[
        EVENT_TYPE_SCOPED_ASSERTION_UPSERTED
    ]["condition"]

    maintenance_bindings = represented[
        PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID
    ]
    maintenance_bindings_by_event = {
        item["event_type"]: item
        for item in maintenance_bindings
        if item["event_type"] != EVENT_TYPE_RELATIONSHIP_ADDED
    }
    assert (
        maintenance_bindings_by_event[EVENT_TYPE_TEXT_RELATION_UPSERTED][
            "input_mapping"
        ]["evidence_kind"]
        == "research_summary"
    )
    relationship_binding = next(
        item
        for item in maintenance_bindings
        if item["event_type"] == EVENT_TYPE_RELATIONSHIP_ADDED
    )
    assert relationship_binding["input_mapping"] == {
        "source_predicate": "event.predicate",
        "source_fingerprint": "event.mutation_id",
    }
    assert relationship_binding["condition"] == {
        "kind": "context_value_in",
        "key": "event.predicate",
        "values": ["#V#has_research_description", "#V#authored_by"],
    }


def test_profile_maintenance_workflow_definition_is_executable_and_separates_writes() -> None:
    bundle = authority_service.load_repo_seed_workflow_bundle(_REPO_SEED_ASSET_PATH)
    definition = authority_service._build_definition_from_publication_spec(
        workflow_id=PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID,
        spec=bundle["publication_specs"][PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID],
    )

    validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=set(bundle["supported_action_ids"]),
        enforce_supported_actions=True,
    )

    assert validation["valid"] is True, validation
    plan_action = definition.states["plan_profile_maintenance"].actions[0]
    assert plan_action.action_id == "llm.action"
    assert plan_action.llm_policy["allowed_tools"] == [
        "fetch_concept",
        "search_concepts",
        "get_text_relations",
        "list_scoped_assertions",
        "find_relations_with_argument",
        "resolve_publication_scope_profile",
    ]
    assert "upsert_scoped_assertion" not in plan_action.llm_policy["allowed_tools"]
    assert plan_action.validation_policy["json_field_defaults"] == {
        "prior_scoped_assertion_id": ""
    }
    assert "prior_scoped_assertion_id" not in plan_action.validation_policy[
        "required_json_fields"
    ]
    assert definition.states["write_global_profile"].actions[0].action_id == (
        "workflow_mcp.invoke_tool"
    )
    replacement_inputs = definition.states[
        "write_replacement_scoped_profile"
    ].actions[0].inputs
    assert replacement_inputs["suppress_event_workflow_launches"] is True
    assert definition.states["retract_prior_scoped_profile"].actions[0].inputs[
        "tool_name"
    ] == "retract_scoped_assertion"


def test_repo_seed_event_binding_publication_updates_and_disables_owned_drift(
    monkeypatch,
) -> None:
    class _Binding:
        def __init__(
            self,
            *,
            binding_id,
            event_type,
            workflow_id,
            enabled=True,
            created_by="paper_recommendation_workflow_vontology_service",
            updated_by="paper_recommendation_workflow_vontology_service",
            condition=None,
            input_mapping=None,
        ):
            self.binding_id = binding_id
            self.event_type = event_type
            self.workflow_id = workflow_id
            self.enabled = enabled
            self.created_by = created_by
            self.updated_by = updated_by
            self.condition = condition
            self.input_mapping = input_mapping or {}

        def to_status_dict(self):
            return {
                "binding_id": self.binding_id,
                "event_type": self.event_type,
                "workflow_id": self.workflow_id,
                "enabled": self.enabled,
                "condition": self.condition,
                "input_mapping": self.input_mapping,
            }

    class _Manager:
        def __init__(self):
            self.bindings = [
                _Binding(
                    binding_id="old-binding",
                    event_type="obsolete.event",
                    workflow_id=PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID,
                ),
                _Binding(
                    binding_id="operator-disabled",
                    event_type="text_relation.upserted",
                    workflow_id=PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID,
                    enabled=False,
                ),
            ]
            self.disabled = []
            self.upserts = []

        def list_event_bindings(self, *, limit):
            assert limit == 500
            return list(self.bindings)

        def set_event_binding_enabled(self, binding_id, *, enabled, actor):
            assert enabled is False
            assert actor == "paper_recommendation_workflow_vontology_service"
            self.disabled.append(binding_id)
            return True

        def upsert_event_binding(self, **kwargs):
            self.upserts.append(kwargs)
            binding = _Binding(
                binding_id=f"new:{kwargs['event_type']}",
                event_type=kwargs["event_type"],
                workflow_id=kwargs["workflow_id"],
                enabled=kwargs["enabled"],
                condition=kwargs["condition"],
                input_mapping=kwargs["input_mapping"],
            )
            return binding, True, False

    manager = _Manager()
    monkeypatch.setattr(
        "src.backend.workflows.durable.startup.get_instance_manager",
        lambda: manager,
    )
    bundle = authority_service.load_repo_seed_workflow_bundle(_REPO_SEED_ASSET_PATH)

    report = seed_bootstrap._apply_repo_seed_event_bindings(
        workflow_event_bindings=bundle["workflow_event_bindings"],
        workflow_ids=[PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID],
        managed_by="paper_recommendation_workflow_vontology_service",
    )

    assert report["success"] is True
    assert report["created_count"] == 3
    assert report["disabled_binding_ids"] == ["old-binding"]
    assert {item["event_type"] for item in report["bindings"]} == {
        "text_relation.upserted",
        "text_relation.updated",
        "relationship.added",
    }
    upserts = {item["event_type"]: item for item in manager.upserts}
    assert upserts["text_relation.upserted"]["enabled"] is False
    assert upserts["text_relation.updated"]["enabled"] is True


def test_profile_maintenance_scoped_replacement_executes_write_retract_readback_path() -> (
    None
):
    bundle = authority_service.load_repo_seed_workflow_bundle(_REPO_SEED_ASSET_PATH)
    definition = authority_service._build_definition_from_publication_spec(
        workflow_id=PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID,
        spec=bundle["publication_specs"][PAPER_MATCHING_PROFILE_MAINTENANCE_WORKFLOW_ID],
    )
    calls: list[dict[str, Any]] = []

    def _invoke_tool(request):
        inputs = dict(request.inputs)
        calls.append(inputs)
        tool_name = inputs["tool_name"]
        if tool_name == "upsert_scoped_assertion":
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "assertion_id": "ska_replacement",
                        "canonical_read_back": {
                            "assertion_id": "ska_replacement",
                            "status": "asserted",
                        },
                    }
                },
            )
        if tool_name == "retract_scoped_assertion":
            return WorkflowActionResult(
                status="success",
                outputs={
                    "result": {
                        "canonical_read_back": {
                            "assertion_id": "ska_prior",
                            "status": "retracted",
                        }
                    }
                },
            )
        assert tool_name == "get_text_relations"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "concept_id": "#V#bin",
                    "context_view": "actor_effective",
                    "relations_found": 1,
                    "relations": [
                        {
                            "row_kind": "scoped_assertion",
                            "assertion_id": "ska_replacement",
                        }
                    ],
                }
            },
        )

    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)
    registry.register(
        ActionSpec(action_id="workflow_mcp.invoke_tool", handler=_invoke_tool)
    )

    result = WorkflowExecutor(registry=registry).run(
        replace(definition, initial_state="serialise_profile"),
        environment=WorkflowEnvironment(
            llm_client=None,
            user_concept_id="#V#michael_witbrock",
            org_concept_id="#V#strong_ai_lab",
        ),
        data={
            "subject_concept_id": "#V#bin",
            "source_artifact_concept_id": "#V#paper_new",
            "evidence_kind": "new_publication",
            "profile_subject_concept_id": "#V#bin",
            "profile_scope_mode": "user",
            "paper_matching_profile": {
                "schema_version": "paper_matching_profile.v1",
                "subject_concept_id": "#V#bin",
                "project_description": "Updated represented summary",
                "stated_interest_terms": ["causal reasoning"],
                "notes": "New publication evidence was incorporated.",
            },
            "prior_scoped_assertion_id": "ska_prior",
            "profile_evidence": {
                "source_artifact_concept_id": "#V#paper_new",
                "evidence_kind": "new_publication",
                "source_fingerprint": "paper-event-1",
            },
            "profile_maintenance_reason": (
                "The represented publication adds material evidence."
            ),
        },
    )

    assert result.completed is True, (
        result.error,
        result.data.get("last_action_error"),
        result.data.get("last_action_outputs"),
        result.data.get("workflow_metadata_validation"),
    )
    assert result.final_state == "completed"
    assert [call["tool_name"] for call in calls] == [
        "upsert_scoped_assertion",
        "retract_scoped_assertion",
        "get_text_relations",
    ]
    assert calls[0]["suppress_event_workflow_launches"] is True
    assert calls[0]["subject_concept_id"] == "#V#bin"
    assert calls[0]["scope_mode"] == "user"
    assert json.loads(calls[0]["target_text"])["stated_interest_terms"] == [
        "causal reasoning"
    ]
    assert calls[1]["assertion_id"] == "ska_prior"
    assert result.data["profile_canonical_readback"]["context_view"] == (
        "actor_effective"
    )
