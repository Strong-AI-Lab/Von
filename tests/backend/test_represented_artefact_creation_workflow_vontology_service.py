from __future__ import annotations

import json
from pathlib import Path
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
from src.backend.services.workflow_repo_seed_bootstrap import (
    bootstrap_repo_seed_workflow_bundle,
)
from src.backend.services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from src.backend.services.workflow_discovery_service import (
    ROUTING_EXCLUSION_EXPLICITLY_DISABLED,
    WorkflowMatch,
    _annotate_and_rank_candidates,
    _filter_routing_candidates,
    assess_workflow_routing_authority,
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows.durable.registry_factory import build_durable_action_registry
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_input_contract,
    resolve_workflow_routing_profile,
)

_SEED_BUNDLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "represented_artefact_creation_workflow_seed_bundle.json"
)
_REVIEWED_V6_PARENT_AUTHORITY_SHA256 = (
    "8d8667342c20b0f97b99ee48bfb9e875568b28ba5ea022d17fd8a69891f3d2db"
)
_REVIEWED_V7_PARENT_AUTHORITY_SHA256 = (
    "76a561b09bf0e4ae165ffb25e775373ef8afb1d79a7cee0859aac2212a28d0f9"
)
_REVIEWED_V7_ITEM_AUTHORITY_SHA256 = (
    "39d510b47d947bcb5d6f77697fa68a770d6c8ed64b579d8f004f382d6d87e532"
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


def _publish_exact_reviewed_v8_parent_authority(tmp_path: Path) -> None:
    """Recreate the released v8 parent before exercising its v6 migration.

    The current v9 seed adds launch/context fields.  Mutating only its lifecycle
    and routing profile would produce a hybrid authority that was never reviewed
    and must correctly be refused by the migration gate.
    """

    legacy_bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    legacy_bundle["seed_version"] = "8"
    legacy_versions = legacy_bundle[
        "known_legacy_authority_payload_sha256_by_seed_version"
    ][REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID]
    legacy_versions.pop("8", None)

    parent = next(
        row
        for row in legacy_bundle["workflows"]
        if row["workflow_id"] == REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    parent["launch_input_contract"]["input_mappings"] = [
        mapping
        for mapping in parent["launch_input_contract"]["input_mappings"]
        if mapping.get("target_context_key")
        not in {"augmented_context", "conversation_situation"}
    ]
    for state in parent["publication_spec"]["steps"]:
        llm_policy = state.get("llm_policy")
        if not isinstance(llm_policy, dict):
            continue
        llm_policy.pop("context_messages_context_key", None)
        llm_policy["context_fields"] = [
            field
            for field in llm_policy.get("context_fields") or []
            if field.get("context_key") != "conversation_situation"
        ]

    legacy_path = tmp_path / "represented_artefact_creation_v8.json"
    legacy_path.write_text(
        json.dumps(legacy_bundle, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    report = bootstrap_repo_seed_workflow_bundle(
        asset_path=legacy_path,
        force_republish=True,
        target_workflow_ids=(REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID,),
    )
    adjudication = (
        ((report.get("publication") or {}).get("repo_seed_version_gate") or {}).get(
            "authority_adjudication_by_workflow"
        )
        or {}
    ).get(REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID) or {}
    assert adjudication["target_authority_payload_sha256"] == (
        _REVIEWED_V7_PARENT_AUTHORITY_SHA256
    )


def _seed_existing_represented_artefact(
    *,
    concept_id: str,
    name: str,
    description: str,
) -> None:
    concept_service.create_concept(
        name=name,
        concept_id=concept_id,
        parent_concept_ids=["#V#workflow_marker"],
        create_as_instance=True,
        visibility_scope_mode="global_general",
    )
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        garbage_collect=True,
    )


def _assert_existing_artefact_was_not_mutated(
    *,
    concept_id: str,
    original_description: str,
    forbidden_description: str,
) -> None:
    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert isinstance(concept_doc, dict)
    type_ids = (concept_doc.get("relationships") or {}).get("is_an_instance_of") or []
    assert "#V#workflow_marker" in type_ids
    assert "#V#workflow_label" not in type_ids

    descriptions = {
        str((row or {}).get("text") or "")
        for row in get_texts_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            limit=20,
        )
    }
    assert original_description in descriptions
    assert forbidden_description not in descriptions


def test_represented_artefact_workflow_family_has_reviewed_parent_only_routing() -> (
    None
):
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))

    assert bundle["seed_version"] == "9"
    assert bundle["known_legacy_authority_payload_sha256_by_seed_version"] == {
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID: {
            "5": ["2061801c8da0d8437f021ef49dd3af40d405f98fdc5388752d0b831ce689bb48"],
            "6": [_REVIEWED_V6_PARENT_AUTHORITY_SHA256],
            "7": [_REVIEWED_V7_PARENT_AUTHORITY_SHA256],
            "8": [_REVIEWED_V7_PARENT_AUTHORITY_SHA256],
        },
        REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID: {
            "5": ["cc17479ee5df029a7baed2b1d21a1f0c098979afcda730c91a2e7acce624771e"],
            "7": [_REVIEWED_V7_ITEM_AUTHORITY_SHA256],
        },
    }

    workflows = {row["workflow_id"]: row for row in bundle["workflows"]}
    parent = workflows[REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID]
    parent_relations = {
        row["predicate"]: json.loads(row["text"]) for row in parent["text_relations"]
    }
    routing = parent_relations["#V#hasWorkflowRoutingProfileJson"]
    lifecycle = parent_relations["#V#hasWorkflowLifecycleJson"]

    assert routing["routing_eligible"] is False
    assert routing["explicit_workflow_context_required"] is True
    assert lifecycle["routing_eligible"] is False
    assert lifecycle["review_state"] == "approved"
    assert lifecycle["reviewed_by"] == "Codex"
    assert lifecycle["review_reason"] == (
        "2026-08-10 explicit-only generic represented-artefact routing repair"
    )

    item = workflows[REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID]
    item_relations = {
        row["predicate"]: json.loads(row["text"]) for row in item["text_relations"]
    }
    item_routing = item_relations["#V#hasWorkflowRoutingProfileJson"]
    item_lifecycle = item_relations["#V#hasWorkflowLifecycleJson"]
    assert item_routing["routing_eligible"] is False
    assert item_routing["explicit_workflow_context_required"] is True
    assert item_lifecycle["routing_eligible"] is False
    assert item_lifecycle["rollout_state"] == "support_subworkflow"
    assert item_lifecycle["review_state"] == "approved"
    assert item["publication_spec"]["initial_state"] == ("initialise_from_item_request")

    parent_sources = {
        mapping.get("source_expression")
        for mapping in parent["launch_input_contract"]["input_mappings"]
    }
    assert "inputs.augmented_context" in parent_sources
    assert "inputs.conversation_situation" in parent_sources
    assert all(
        "inputs.conversation_situation"
        not in {
            mapping.get("source_expression")
            for mapping in row["launch_input_contract"]["input_mappings"]
        }
        for row in (item,)
    )


def test_normal_bootstrap_migrates_exact_reviewed_v6_parent_authority(
    tmp_path: Path,
) -> None:
    bootstrap_canonical_represented_artefact_creation_workflow()
    _publish_exact_reviewed_v8_parent_authority(tmp_path)

    upsert_singleton_text_relation(
        subject_concept_id=REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRoutingProfileJson",
        text=json.dumps(
            {
                "authoring_intent_required": False,
                "prefer_existing_capability": False,
                "role": "execution",
                "schema_version": "workflow_routing_profile.v1",
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        lang="en-NZ",
        context={"source": "test_reviewed_v6_parent_authority"},
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLifecycleJson",
        text=json.dumps(
            {
                "phase": "published",
                "published": True,
                "schema_version": "workflow_publication_lifecycle.v1",
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        lang="en-NZ",
        context={"source": "test_reviewed_v6_parent_authority"},
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRepoSeedVersionJson",
        text=json.dumps(
            {
                "authority_payload_sha256": _REVIEWED_V6_PARENT_AUTHORITY_SHA256,
                "family_id": "represented_artefact_creation_workflow_seed_bundle",
                "schema_version": "workflow_repo_seed_version.v1",
                "seed_version": "6",
                "source_tag": "JVNAUTOSCI-2577",
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        lang="en-NZ",
        context={"source": "test_reviewed_v6_parent_authority"},
        garbage_collect=True,
    )
    invalidate_workflow_discovery_executability_caches()

    report = bootstrap_canonical_represented_artefact_creation_workflow()
    publication = report.get("publication") or {}
    gate = publication.get("repo_seed_version_gate") or {}
    adjudication = (gate.get("authority_adjudication_by_workflow") or {}).get(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    ) or {}

    assert adjudication["reason"] == "exact_reviewed_legacy_migration"
    assert adjudication["observed_authority_payload_sha256"] == (
        _REVIEWED_V6_PARENT_AUTHORITY_SHA256
    )
    assert publication["counts"]["workflows_published"] == 1
    assert publication["counts"]["errors"] == 0

    routing_profile, _ = resolve_workflow_routing_profile(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert routing_profile["routing_eligible"] is False
    assert routing_profile["explicit_workflow_context_required"] is True


def test_normal_bootstrap_migrates_exact_reviewed_v7_item_authority() -> None:
    bootstrap_canonical_represented_artefact_creation_workflow()

    upsert_singleton_text_relation(
        subject_concept_id=REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRoutingProfileJson",
        text=json.dumps(
            {
                "authoring_intent_required": False,
                "prefer_existing_capability": False,
                "role": "execution",
                "schema_version": "workflow_routing_profile.v1",
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        lang="en-NZ",
        context={"source": "test_reviewed_v7_item_authority"},
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLifecycleJson",
        text=json.dumps(
            {
                "phase": "published",
                "published": True,
                "schema_version": "workflow_publication_lifecycle.v1",
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        lang="en-NZ",
        context={"source": "test_reviewed_v7_item_authority"},
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRepoSeedVersionJson",
        text=json.dumps(
            {
                "authority_payload_sha256": _REVIEWED_V7_ITEM_AUTHORITY_SHA256,
                "family_id": "represented_artefact_creation_workflow_seed_bundle",
                "schema_version": "workflow_repo_seed_version.v1",
                "seed_version": "7",
                "source_tag": "JVNAUTOSCI-2577",
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        lang="en-NZ",
        context={"source": "test_reviewed_v7_item_authority"},
        garbage_collect=True,
    )
    invalidate_workflow_discovery_executability_caches()

    report = bootstrap_canonical_represented_artefact_creation_workflow()
    publication = report.get("publication") or {}
    gate = publication.get("repo_seed_version_gate") or {}
    adjudication = (gate.get("authority_adjudication_by_workflow") or {}).get(
        REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID
    ) or {}

    assert adjudication["reason"] == "exact_reviewed_legacy_migration"
    assert adjudication["observed_authority_payload_sha256"] == (
        _REVIEWED_V7_ITEM_AUTHORITY_SHA256
    )
    assert publication["counts"]["workflows_published"] == 1
    assert publication["counts"]["errors"] == 0

    routing_profile, _ = resolve_workflow_routing_profile(
        REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID
    )
    assert routing_profile["routing_eligible"] is False
    assert routing_profile["explicit_workflow_context_required"] is True


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
    assert routing_profile.get("routing_eligible") is False
    assert routing_profile.get("explicit_workflow_context_required") is True

    routing_authority = assess_workflow_routing_authority(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID,
        query="Keep the useful travel details somewhere sensible.",
    )
    assert routing_authority["routing_eligible"] is False
    assert routing_authority["routing_exclusion_reason"] == (
        ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    )

    item_routing_profile, item_routing_source = resolve_workflow_routing_profile(
        REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID
    )
    assert isinstance(item_routing_profile, dict)
    assert item_routing_source.startswith("text_relation:")
    assert item_routing_profile.get("role") == "execution"
    assert item_routing_profile.get("routing_eligible") is False
    assert item_routing_profile.get("explicit_workflow_context_required") is True

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
    assert len(parent_required_effects) == 2
    parent_effects_by_id = {
        effect["effect_id"]: effect
        for effect in parent_required_effects
        if isinstance(effect, dict)
    }
    assert parent_effects_by_id["represented_artefact_readback"]["required_tools"] == [
        "fetch_concept",
        "get_text_relations_summary",
    ]
    assert parent_effects_by_id["represented_artefact_create_parent_assertion"][
        "required_tools"
    ] == ["add_relationship"]
    assert parent_effects_by_id["represented_artefact_create_parent_assertion"][
        "activation_required_tools"
    ] == ["create_concepts"]

    required_effects_contract = item_definition.metadata.get(
        "required_effects_contract"
    )
    assert isinstance(required_effects_contract, dict)
    required_effects = required_effects_contract.get("required_effects") or []
    item_effects_by_id = {
        effect["effect_id"]: effect
        for effect in required_effects
        if isinstance(effect, dict)
    }
    assert item_effects_by_id["represented_artefact_readback"]["required_tools"] == [
        "fetch_concept",
        "get_text_relations_summary",
    ]
    assert item_effects_by_id["represented_artefact_create_parent_assertion"][
        "required_tools"
    ] == ["add_relationship"]
    assert item_effects_by_id["represented_artefact_create_parent_assertion"][
        "activation_required_tools"
    ] == ["create_concepts"]

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

    assert item_definition.initial_state == _item_step_id(
        "initialise_from_item_request"
    )
    initialise_action = item_definition.states[
        _item_step_id("initialise_from_item_request")
    ].actions[0]
    assert initialise_action.action_id == "workflow_control.context_set"
    assignments = initialise_action.inputs.get("assignments")
    assert isinstance(assignments, list)
    assert any(
        item.get("key") == "represented_artefact_create_concepts"
        and item.get("value_from_context")
        == "current_represented_artefact_request.concepts"
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
    assert llm_policy.get("allowed_tools") == [
        "resolve_concept_by_name",
        "search_concepts",
        "fetch_concept",
    ]
    assert llm_policy.get("required_tools") == ["resolve_concept_by_name"]
    assert any(
        field.get("context_key") == "current_represented_artefact_request"
        for field in llm_policy.get("context_fields", [])
        if isinstance(field, dict)
    )
    assert "Never use #V#thing as parent" in llm_policy.get(
        "response_contract_text",
        "",
    )

    outer_reuse_transition = next(
        transition
        for transition in definition.states[
            _step_id("plan_represented_artefact")
        ].transitions
        if transition.reason == "reuse_existing_verified"
    )
    assert outer_reuse_transition.to_state == _step_id("read_back_concept")

    outer_plan_policy = (
        definition.states[_step_id("plan_represented_artefact")].actions[0].llm_policy
    )
    assert isinstance(outer_plan_policy, dict)
    assert outer_plan_policy.get("required_tools") == ["resolve_concept_by_name"]
    assert set(outer_plan_policy.get("allowed_tools") or []) == {
        "resolve_concept_by_name",
        "search_concepts",
        "fetch_concept",
    }

    item_initialise_reuse_transition = next(
        transition
        for transition in item_definition.states[
            _item_step_id("initialise_from_item_request")
        ].transitions
        if transition.reason == "preplanned_reuse_existing_verified"
    )
    assert item_initialise_reuse_transition.to_state == _item_step_id(
        "read_back_concept"
    )
    item_plan_reuse_transition = next(
        transition
        for transition in item_definition.states[
            _item_step_id("plan_represented_artefact")
        ].transitions
        if transition.reason == "reuse_existing_verified"
    )
    assert item_plan_reuse_transition.to_state == _item_step_id("read_back_concept")
    item_plan_policy = (
        item_definition.states[_item_step_id("plan_represented_artefact")]
        .actions[0]
        .llm_policy
    )
    assert isinstance(item_plan_policy, dict)
    assert item_plan_policy.get("required_tools") == ["resolve_concept_by_name"]
    assert set(item_plan_policy.get("allowed_tools") or []) == {
        "resolve_concept_by_name",
        "search_concepts",
        "fetch_concept",
    }

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

    attach_action = item_definition.states[_item_step_id("attach_description")].actions[
        0
    ]
    assert attach_action.action_id == "upsert_singleton_text_relation"
    assert attach_action.inputs.get("predicate") == "hasDescription"

    read_back_action = item_definition.states[
        _item_step_id("read_back_concept")
    ].actions[0]
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


def test_internal_item_workflow_is_excluded_from_ordinary_routing_candidates() -> None:
    bootstrap_canonical_represented_artefact_creation_workflow()

    ordinary_query = (
        "That design makes sense. Go ahead, reusing anything that's already there."
    )
    routing_authority = assess_workflow_routing_authority(
        REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID,
        query=ordinary_query,
    )
    assert routing_authority["routing_eligible"] is False
    assert routing_authority["routing_exclusion_reason"] == (
        ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    )

    annotated = _annotate_and_rank_candidates(
        [
            WorkflowMatch(
                concept_id=REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID,
                name="Represented Artefact Item Creation Workflow",
                relevance_score=1.0,
            )
        ],
        max_results=1,
        query=ordinary_query,
    )
    assert len(annotated) == 1
    assert annotated[0].is_executable is True
    assert annotated[0].routing_eligible is False
    assert annotated[0].is_policy_safe is False
    assert annotated[0].routing_exclusion_reason == (
        ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    )
    assert (
        _filter_routing_candidates(
            annotated,
            allow_non_executable=False,
        )
        == []
    )


def test_parent_fanout_can_invoke_non_routing_internal_item_workflow() -> None:
    bootstrap_canonical_represented_artefact_creation_workflow()
    definition = load_workflow_definition_from_vontology(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert definition is not None

    artefact_specs = []
    for suffix in ("a", "b"):
        concept_id = f"#V#existing_parent_fanout_marker_{suffix}"
        name = f"Existing parent fan-out marker {suffix.upper()}"
        description = f"Original parent fan-out description {suffix.upper()}."
        _seed_existing_represented_artefact(
            concept_id=concept_id,
            name=name,
            description=description,
        )
        artefact_specs.append(
            {
                "name": name,
                "decision": "reuse_existing",
                "target_name": name,
                "target_code": f"existing/parent_fanout/{suffix}",
                "target_kind": "individual",
                "parent_id": None,
                "existing_concept_id": concept_id,
                "concepts": [],
                "description_text": description,
                "parent_rationale": "Reuse the already represented marker.",
                "blocking_reason": None,
                "prompt": f"Reuse {name} and read it back.",
            }
        )

    queued_llm = _QueuedLLM(
        [
            json.dumps(
                {
                    "mode": "set",
                    "artefact_specs": artefact_specs,
                    "set_summary": "Two existing represented markers.",
                    "blocking_reason": None,
                }
            )
        ]
    )
    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=20,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=queued_llm,
            user_namespace="#V#test_user",
        ),
        data={"prompt": "Reuse these two represented markers."},
    )

    assert result.completed is True
    assert result.final_state == _step_id("completed")
    assert queued_llm.remaining_count == 0
    assert result.data.get("represented_artefact_set_success_count") == 2
    assert result.data.get("represented_artefact_set_error_count") == 0
    invocations = result.data.get("invocations") or []
    action_ids = {item.get("tool") for item in invocations if isinstance(item, dict)}
    assert {"fetch_concept", "get_text_relations_summary"}.issubset(action_ids)
    assert action_ids.isdisjoint(
        {"create_concepts", "add_relationship", "upsert_singleton_text_relation"}
    )


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
    assert "Reuse is read-only" in prompt_text
    assert "Use `resolve_concept_by_name` for the exact supplied artefact name" in (
        prompt_text
    )
    assert "without adding relationships or replacing descriptions" in prompt_text

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


def test_represented_artefact_prompt_bootstrap_preserves_live_authority_until_forced() -> (
    None
):
    first_report = _ensure_represented_artefact_creation_prompt_support()
    assert first_report.get("success") is True

    custom_prompt = "Custom live represented-artefact policy authored in Vontology."
    upsert_singleton_text_relation(
        subject_concept_id=REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        text=custom_prompt,
        lang="en-NZ",
        context={"source": "human_vontology_author"},
        garbage_collect=True,
    )

    normal_report = _ensure_represented_artefact_creation_prompt_support()
    assert normal_report.get("success") is True
    assert normal_report.get("seeded_prompt_count") == 0
    normal_rows = get_texts_for_concept(
        REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert [row.get("text") for row in normal_rows] == [custom_prompt]

    forced_report = _ensure_represented_artefact_creation_prompt_support(
        force_prompt_seed=True
    )
    assert forced_report.get("success") is True
    assert forced_report.get("seeded_prompt_count") == 2
    forced_rows = get_texts_for_concept(
        REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert [row.get("text") for row in forced_rows] != [custom_prompt]


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
            llm_client=_QueuedLLM(
                [json.dumps(extraction_payload), json.dumps(payload)]
            ),
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
        isinstance(row, dict) and (row.get("text") or "") == payload["description_text"]
        for row in description_rows
    )
    readback = result.data.get("represented_artefact_text_relation_summary") or {}
    assert readback.get("success") is True
    assert readback.get("groups_found", 0) >= 1
    response_text = str(result.data.get("response_text") or "")
    assert "Decision: create" in response_text
    assert "mutation steps are confined to the create path" in response_text


def test_outer_reuse_existing_path_reads_without_mutating_existing_artefact() -> None:
    bootstrap_canonical_represented_artefact_creation_workflow()
    definition = load_workflow_definition_from_vontology(
        REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID
    )
    assert definition is not None

    concept_id = "#V#existing_outer_reuse_marker"
    original_description = "Original outer reuse description."
    forbidden_description = "Replacement outer reuse description."
    _seed_existing_represented_artefact(
        concept_id=concept_id,
        name="Existing outer reuse marker",
        description=original_description,
    )

    extraction_payload = {
        "mode": "single",
        "artefact_specs": [],
        "set_summary": None,
        "blocking_reason": None,
    }
    reuse_payload = {
        "decision": "reuse_existing",
        "target_name": "Existing outer reuse marker",
        "target_code": "existing/outer",
        "target_kind": "individual",
        "parent_id": "#V#workflow_label",
        "existing_concept_id": concept_id,
        "concepts": [],
        "description_text": forbidden_description,
        "parent_rationale": "A deliberately different parent for the regression.",
        "blocking_reason": None,
    }

    queued_llm = _QueuedLLM([json.dumps(extraction_payload), json.dumps(reuse_payload)])
    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=20,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=queued_llm,
            user_namespace="#V#test_user",
        ),
        data={"prompt": "Reuse the represented marker if it already exists."},
    )

    assert result.completed is True
    assert result.final_state == _step_id("completed")
    assert queued_llm.remaining_count == 0
    step_envelopes = result.data.get("workflow_step_result_envelopes") or []
    action_ids = {
        item.get("action_id") for item in step_envelopes if isinstance(item, dict)
    }
    assert {"fetch_concept", "get_text_relations_summary"}.issubset(action_ids)
    assert action_ids.isdisjoint(
        {"create_concepts", "add_relationship", "upsert_singleton_text_relation"}
    )
    assert "Decision: reuse_existing" in str(result.data.get("response_text") or "")
    _assert_existing_artefact_was_not_mutated(
        concept_id=concept_id,
        original_description=original_description,
        forbidden_description=forbidden_description,
    )


def test_fanout_item_reuse_existing_path_reads_without_mutating_existing_artefact() -> (
    None
):
    bootstrap_canonical_represented_artefact_creation_workflow()
    item_definition = load_workflow_definition_from_vontology(
        REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID
    )
    assert item_definition is not None

    concept_id = "#V#existing_fanout_reuse_marker"
    original_description = "Original fan-out reuse description."
    forbidden_description = "Replacement fan-out reuse description."
    _seed_existing_represented_artefact(
        concept_id=concept_id,
        name="Existing fan-out reuse marker",
        description=original_description,
    )
    item_request = {
        "decision": "reuse_existing",
        "target_name": "Existing fan-out reuse marker",
        "target_code": "existing/fanout",
        "target_kind": "individual",
        "parent_id": None,
        "existing_concept_id": concept_id,
        "concepts": [],
        "description_text": forbidden_description,
        "parent_rationale": "A deliberately different parent for the regression.",
        "blocking_reason": None,
        "prompt": "Reuse the existing fan-out marker and read it back.",
    }

    queued_llm = _QueuedLLM([])
    result = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=15,
    ).run(
        item_definition,
        environment=WorkflowEnvironment(
            llm_client=queued_llm,
            user_namespace="#V#test_user",
        ),
        data={"current_represented_artefact_request": item_request},
    )

    assert result.completed is True
    assert result.final_state == _item_step_id("completed")
    assert queued_llm.remaining_count == 0
    step_envelopes = result.data.get("workflow_step_result_envelopes") or []
    action_ids = {
        item.get("action_id") for item in step_envelopes if isinstance(item, dict)
    }
    assert {"fetch_concept", "get_text_relations_summary"}.issubset(action_ids)
    assert action_ids.isdisjoint(
        {"create_concepts", "add_relationship", "upsert_singleton_text_relation"}
    )
    assert "Decision: reuse_existing" in str(result.data.get("response_text") or "")
    _assert_existing_artefact_was_not_mutated(
        concept_id=concept_id,
        original_description=original_description,
        forbidden_description=forbidden_description,
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
    invocation_tools = [
        item.get("tool") for item in invocations if isinstance(item, dict)
    ]
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
