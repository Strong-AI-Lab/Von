from __future__ import annotations

import json
from typing import Any

import pytest

from src.backend.db.mongo_client import get_db
from src.backend.services import concept_service
from src.backend.services.represented_artefact_creation_workflow_vontology_service import (
    REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID,
    REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID,
    REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID,
    REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID,
    _ensure_represented_artefact_creation_prompt_support,
    bootstrap_canonical_represented_artefact_creation_workflow,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows.durable.registry_factory import build_durable_action_registry
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_input_contract,
    resolve_workflow_routing_profile,
)


class _QueuedLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def generate(
        self,
        prompt: str,
        context: Any = None,
        model: str | None = None,
        llm_params: dict[str, Any] | None = None,
    ) -> str:
        _ = (prompt, context, model, llm_params)
        if not self._responses:
            raise AssertionError("queued_llm_exhausted")
        return self._responses.pop(0)

    @property
    def remaining_count(self) -> int:
        return len(self._responses)


@pytest.fixture(autouse=True)
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    invalidate_workflow_discovery_executability_caches()
    yield
    invalidate_workflow_discovery_executability_caches()
    authority_service.clear_workflow_type_resolution_cache()


def _step_id(state_id: str) -> str:
    return _workflow_step_id(REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID, state_id)


def _item_step_id(state_id: str) -> str:
    return _workflow_step_id(REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID, state_id)


def _workflow_step_id(workflow_id: str, state_id: str) -> str:
    return authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id=state_id,
    )


def test_bootstrap_materialises_represented_artefact_creation_workflow() -> None:
    report = bootstrap_canonical_represented_artefact_creation_workflow()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 2
    assert counts.get("errors") == 0
    support_concepts = report.get("support_concepts") or {}
    assert "#V#workflow_marker" in support_concepts.get("created_concept_ids", [])
    assert "#V#paper_suggestion_provenance_fact" in support_concepts.get(
        "created_concept_ids",
        [],
    )
    assert support_concepts.get("errors") == []

    definition = load_workflow_definition_from_vontology(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert definition is not None
    assert definition.initial_state == _step_id("extract_represented_artefact_set")
    item_definition = load_workflow_definition_from_vontology(
        REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID
    )
    assert item_definition is not None
    assert item_definition.initial_state == _item_step_id(
        "initialise_from_item_request"
    )

    routing_profile, routing_source = resolve_workflow_routing_profile(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert isinstance(routing_profile, dict)
    assert routing_source.startswith("text_relation:")
    assert routing_profile.get("role") == "execution"

    discovery_exemplars, discovery_source = resolve_workflow_discovery_exemplars(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert isinstance(discovery_exemplars, dict)
    assert discovery_source.startswith("text_relation:")
    assert "represented artefact creation" in (
        discovery_exemplars.get("keywords") or []
    )
    assert "paper suggested by" in (discovery_exemplars.get("keywords") or [])

    launch_contract, launch_source = resolve_workflow_launch_input_contract(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert isinstance(launch_contract, dict)
    assert launch_source.startswith("text_relation:")
    assert launch_contract.get("required_inputs") == ["prompt"]

    parent_required_effects_contract = definition.metadata.get(
        "required_effects_contract"
    )
    assert isinstance(parent_required_effects_contract, dict)
    parent_required_effects = (
        parent_required_effects_contract.get("required_effects") or []
    )
    assert len(parent_required_effects) == 1
    assert parent_required_effects[0]["required_tools"] == [
        "add_relationship",
        "fetch_concept",
        "get_text_relations_summary",
    ]

    required_effects_contract = item_definition.metadata.get(
        "required_effects_contract"
    )
    assert isinstance(required_effects_contract, dict)
    required_effects = required_effects_contract.get("required_effects") or []
    assert required_effects[0]["required_tools"] == [
        "add_relationship",
        "fetch_concept",
        "get_text_relations_summary",
    ]

    extract_action = definition.states[
        _step_id("extract_represented_artefact_set")
    ].actions[0]
    assert extract_action.action_id == "llm.action"
    assert extract_action.execution_mode == "llm"
    assert extract_action.validation_policy == {"output_format": "json_value"}
    prompt_contract = extract_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("resolved_prompt_concept_id") == (
        REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID
    )
    extract_llm_policy = extract_action.llm_policy
    assert isinstance(extract_llm_policy, dict)
    assert extract_llm_policy.get("tool_mode") == "none"

    fanout_action = definition.states[
        _step_id("create_represented_artefact_set")
    ].actions[0]
    assert fanout_action.action_id == "workflow_control.for_each"
    assert fanout_action.inputs.get("workflow_id") == (
        REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID
    )
    assert fanout_action.inputs.get("items_context_key") == "represented_artefact_items"
    assert fanout_action.inputs.get("item_context_key") == (
        "current_represented_artefact_request"
    )
    assert fanout_action.inputs.get("max_concurrency") == 6

    assert item_definition.initial_state == _item_step_id("initialise_from_item_request")
    initialise_action = item_definition.states[
        _item_step_id("initialise_from_item_request")
    ].actions[0]
    assert initialise_action.action_id == "workflow_control.context_set"
    assignments = initialise_action.inputs.get("assignments")
    assert isinstance(assignments, list)
    assert any(
        item.get("key") == "represented_artefact_create_concepts"
        and item.get("value_from_context") == "current_represented_artefact_request.concepts"
        for item in assignments
        if isinstance(item, dict)
    )

    plan_action = item_definition.states[
        _item_step_id("plan_represented_artefact")
    ].actions[0]
    assert plan_action.action_id == "llm.action"
    assert plan_action.execution_mode == "llm"
    assert plan_action.validation_policy == {"output_format": "json_value"}
    llm_policy = plan_action.llm_policy
    assert isinstance(llm_policy, dict)
    assert llm_policy.get("tool_mode") == "allowed"
    assert llm_policy.get("allowed_tools") == ["search_concepts", "fetch_concept"]
    assert llm_policy.get("required_tools") == ["search_concepts"]
    assert any(
        field.get("context_key") == "current_represented_artefact_request"
        for field in llm_policy.get("context_fields", [])
        if isinstance(field, dict)
    )
    assert "Never use #V#thing as parent" in llm_policy.get(
        "response_contract_text",
        "",
    )

    create_state = item_definition.states[_item_step_id("create_represented_artefact")]
    create_action = create_state.actions[0]
    assert create_action.action_id == "create_concepts"
    assert create_action.inputs.get("parent_id") == {
        "$context_key": "represented_artefact_parent_id",
        "$mapping_concept_id": (
            "#V#workflow_mapping_represented_artefact_item_creation_create_parent_to_parent_id_parameter"
        ),
        "$required": True,
    }
    assert create_action.inputs.get("concepts") == {
        "$context_key": "represented_artefact_create_concepts",
        "$mapping_concept_id": (
            "#V#workflow_mapping_represented_artefact_item_creation_create_concepts_to_concepts_parameter"
        ),
        "$required": True,
    }
    assert create_state.metadata.get("mutation_authority") == {
        "maximum_level": "additive_vontology",
        "reason_code": "represented_artefact_creation_additive_write",
        "schema_version": "workflow_step_mutation_authority.v1",
    }

    assert_parent_state = item_definition.states[
        _item_step_id("assert_parent_relationship")
    ]
    assert_parent_action = assert_parent_state.actions[0]
    assert assert_parent_action.action_id == "add_relationship"
    assert assert_parent_action.inputs.get("source_id") == {
        "$context_key": "represented_artefact_concept_id",
        "$mapping_concept_id": (
            "#V#workflow_mapping_represented_artefact_item_creation_assert_parent_source_to_source_id_parameter"
        ),
        "$required": True,
    }
    assert assert_parent_action.inputs.get("predicate") == "instance_of"
    assert assert_parent_action.inputs.get("target") == {
        "$context_key": "represented_artefact_parent_id",
        "$mapping_concept_id": (
            "#V#workflow_mapping_represented_artefact_item_creation_assert_parent_target_to_target_parameter"
        ),
        "$required": True,
    }
    assert assert_parent_state.metadata.get("mutation_authority") == {
        "maximum_level": "additive_vontology",
        "reason_code": "represented_artefact_parent_membership_write",
        "schema_version": "workflow_step_mutation_authority.v1",
    }

    attach_action = item_definition.states[_item_step_id("attach_description")].actions[0]
    assert attach_action.action_id == "upsert_singleton_text_relation"
    assert attach_action.inputs.get("predicate") == "hasDescription"

    read_back_action = item_definition.states[_item_step_id("read_back_concept")].actions[0]
    assert read_back_action.action_id == "fetch_concept"
    text_read_back_action = item_definition.states[
        _item_step_id("read_back_text_relations")
    ].actions[0]
    assert text_read_back_action.action_id == "get_text_relations_summary"
    assert text_read_back_action.inputs.get("predicates") == [
        "hasName",
        "hasDescription",
        "hasContent",
        "hasNote",
    ]


def test_represented_artefact_creation_prompt_support_seeds_parent_policy() -> None:
    report = _ensure_represented_artefact_creation_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 2

    rows = get_texts_for_concept(
        REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(prompt_text, str)
    assert "#V#workflow_marker" in prompt_text
    assert "#V#workflow_label" in prompt_text
    assert "#V#paper_suggestion_provenance_fact" in prompt_text
    assert "#V#research_lab_artefact" in prompt_text
    assert "current_represented_artefact_request" in prompt_text
    assert "parent_resolution_required" in prompt_text
    assert "Do not use `#V#thing` as the parent" in prompt_text

    set_rows = get_texts_for_concept(
        REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    set_prompt_text = next(
        ((row or {}).get("text") for row in set_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(set_prompt_text, str)
    assert "artefact_specs" in set_prompt_text
    assert "Do not call tools in this step" in set_prompt_text


def test_represented_artefact_workflow_executes_create_and_readback_path() -> None:
    bootstrap_canonical_represented_artefact_creation_workflow()
    definition = load_workflow_definition_from_vontology(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert definition is not None

    extraction_payload = {
        "mode": "single",
        "artefact_specs": [],
        "set_summary": None,
        "blocking_reason": None,
    }
    payload = {
        "decision": "create",
        "target_name": "Synthetic processed suggestion marker",
        "target_code": "synthetic/processed",
        "target_kind": "individual",
        "parent_id": "#V#workflow_marker",
        "existing_concept_id": None,
        "concepts": [
            {
                "name": "Synthetic processed suggestion marker",
                "kind": "individual",
                "description": "Code: synthetic/processed. Meaning: test marker.",
            }
        ],
        "description_text": "Code: synthetic/processed. Meaning: test marker.",
        "parent_rationale": "Workflow marker requested.",
        "blocking_reason": None,
    }

    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=20,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM([json.dumps(extraction_payload), json.dumps(payload)]),
            user_namespace="#V#test_user",
        ),
        data={"prompt": "Represent marker"},
    )

    assert result.completed is True
    assert result.final_state == _step_id("completed")
    assert result.data.get("represented_artefact_parent_id_used") == (
        "#V#workflow_marker"
    )
    concept_id = str(result.data.get("represented_artefact_concept_id") or "").strip()
    assert concept_id == "#V#synthetic_processed_suggestion_marker"

    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert isinstance(concept_doc, dict)
    type_ids = (concept_doc.get("relationships") or {}).get("is_an_instance_of") or []
    assert "#V#workflow_marker" in type_ids

    description_rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate="hasDescription",
        limit=20,
    )
    assert any(
        isinstance(row, dict)
        and (row.get("text") or "") == payload["description_text"]
        for row in description_rows
    )
    readback = result.data.get("represented_artefact_text_relation_summary") or {}
    assert readback.get("success") is True
    assert readback.get("groups_found", 0) >= 1
    assert "add_relationship, fetch_concept, and get_text_relations_summary" in str(
        result.data.get("response_text") or ""
    )


def test_represented_artefact_workflow_fans_out_set_items() -> None:
    bootstrap_canonical_represented_artefact_creation_workflow()
    definition = load_workflow_definition_from_vontology(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert definition is not None

    set_payload = {
        "mode": "set",
        "artefact_specs": [
            {
                "name": "Synthetic workflow label A",
                "code": "synthetic/label_a",
                "kind_hint": "workflow label",
                "meaning": "First synthetic label for fan-out testing.",
                "parent_hint": "#V#workflow_label",
                "hierarchy_hint": None,
                "association_hint": "JVNAUTOSCI-2247 regression fixture",
                "provenance": "test prompt",
                "decision": "create",
                "target_name": "Synthetic workflow label A",
                "target_code": "synthetic/label_a",
                "target_kind": "individual",
                "parent_id": "#V#workflow_label",
                "existing_concept_id": None,
                "concepts": [
                    {
                        "name": "Synthetic workflow label A",
                        "kind": "individual",
                        "description": (
                            "Code: synthetic/label_a. Meaning: First synthetic label "
                            "for fan-out testing."
                        ),
                    }
                ],
                "description_text": (
                    "Code: synthetic/label_a. Meaning: First synthetic label for "
                    "fan-out testing."
                ),
                "parent_rationale": "Workflow label requested.",
                "blocking_reason": None,
                "prompt": (
                    "Represent one workflow label named Synthetic workflow label A "
                    "with code synthetic/label_a and meaning First synthetic label "
                    "for fan-out testing."
                ),
            },
            {
                "name": "Synthetic workflow label B",
                "code": "synthetic/label_b",
                "kind_hint": "workflow label",
                "meaning": "Second synthetic label for fan-out testing.",
                "parent_hint": "#V#workflow_label",
                "hierarchy_hint": None,
                "association_hint": "JVNAUTOSCI-2247 regression fixture",
                "provenance": "test prompt",
                "decision": "create",
                "target_name": "Synthetic workflow label B",
                "target_code": "synthetic/label_b",
                "target_kind": "individual",
                "parent_id": "#V#workflow_label",
                "existing_concept_id": None,
                "concepts": [
                    {
                        "name": "Synthetic workflow label B",
                        "kind": "individual",
                        "description": (
                            "Code: synthetic/label_b. Meaning: Second synthetic label "
                            "for fan-out testing."
                        ),
                    }
                ],
                "description_text": (
                    "Code: synthetic/label_b. Meaning: Second synthetic label for "
                    "fan-out testing."
                ),
                "parent_rationale": "Workflow label requested.",
                "blocking_reason": None,
                "prompt": (
                    "Represent one workflow label named Synthetic workflow label B "
                    "with code synthetic/label_b and meaning Second synthetic label "
                    "for fan-out testing."
                ),
            },
        ],
        "set_summary": "Two synthetic workflow labels.",
        "blocking_reason": None,
    }
    item_payloads = [
        {
            "decision": "create",
            "target_name": "Synthetic workflow label A",
            "target_code": "synthetic/label_a",
            "target_kind": "individual",
            "parent_id": "#V#workflow_label",
            "existing_concept_id": None,
            "concepts": [
                {
                    "name": "Synthetic workflow label A",
                    "kind": "individual",
                    "description": (
                        "Code: synthetic/label_a. Meaning: First synthetic label "
                        "for fan-out testing."
                    ),
                }
            ],
            "description_text": (
                "Code: synthetic/label_a. Meaning: First synthetic label for "
                "fan-out testing."
            ),
            "parent_rationale": "Workflow label requested.",
            "blocking_reason": None,
        },
        {
            "decision": "create",
            "target_name": "Synthetic workflow label B",
            "target_code": "synthetic/label_b",
            "target_kind": "individual",
            "parent_id": "#V#workflow_label",
            "existing_concept_id": None,
            "concepts": [
                {
                    "name": "Synthetic workflow label B",
                    "kind": "individual",
                    "description": (
                        "Code: synthetic/label_b. Meaning: Second synthetic label "
                        "for fan-out testing."
                    ),
                }
            ],
            "description_text": (
                "Code: synthetic/label_b. Meaning: Second synthetic label for "
                "fan-out testing."
            ),
            "parent_rationale": "Workflow label requested.",
            "blocking_reason": None,
        },
    ]

    queued_llm = _QueuedLLM([json.dumps(set_payload), *map(json.dumps, item_payloads)])

    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=queued_llm,
            user_namespace="#V#test_user",
        ),
        data={"prompt": "Represent two workflow labels"},
    )

    assert result.completed is True
    assert result.final_state == _step_id("completed")
    assert queued_llm.remaining_count == len(item_payloads)
    assert result.data.get("represented_artefact_set_success_count") == 2
    assert result.data.get("represented_artefact_set_error_count") == 0
    invocations = result.data.get("invocations")
    assert isinstance(invocations, list)
    invocation_tools = [item.get("tool") for item in invocations if isinstance(item, dict)]
    assert "create_concepts" in invocation_tools
    assert "add_relationship" in invocation_tools
    assert "upsert_singleton_text_relation" in invocation_tools
    assert "fetch_concept" in invocation_tools
    assert "get_text_relations_summary" in invocation_tools
    iteration_results = result.data.get("represented_artefact_iteration_results")
    assert isinstance(iteration_results, list)
    assert len(iteration_results) == 2

    for expected_id, expected_description in [
        ("#V#synthetic_workflow_label_a", item_payloads[0]["description_text"]),
        ("#V#synthetic_workflow_label_b", item_payloads[1]["description_text"]),
    ]:
        concept_doc = concept_service.get_concept_by_concept_id(expected_id)
        assert isinstance(concept_doc, dict)
        type_ids = (concept_doc.get("relationships") or {}).get(
            "is_an_instance_of"
        ) or []
        assert "#V#workflow_label" in type_ids
        description_rows = get_texts_for_concept(
            subject_concept_id=expected_id,
            predicate="hasDescription",
            limit=20,
        )
        assert any(
            isinstance(row, dict) and (row.get("text") or "") == expected_description
            for row in description_rows
        )

    assert "Represented artefact set verified" in str(
        result.data.get("response_text") or ""
    )
