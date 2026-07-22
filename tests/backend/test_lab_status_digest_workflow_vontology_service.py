from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.lab_status_digest_workflow_vontology_service import (
    LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID,
    LAB_STATUS_DIGEST_WORKFLOW_ID,
    LAB_STATUS_DIGEST_WORK_PRODUCT_ID,
    _ensure_lab_status_digest_prompt_support,
    bootstrap_canonical_lab_status_digest_workflow,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_input_contract,
    resolve_workflow_routing_profile,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

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


def test_bootstrap_materialises_lab_status_digest_authority(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_lab_status_digest_workflow()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 1
    assert counts.get("errors") == 0

    definition = load_workflow_definition_from_vontology(
        LAB_STATUS_DIGEST_WORKFLOW_ID
    )
    assert definition is not None

    routing_profile, routing_source = resolve_workflow_routing_profile(
        LAB_STATUS_DIGEST_WORKFLOW_ID
    )
    assert routing_source.startswith("text_relation:")
    assert routing_profile.get("schema_version") == "workflow_routing_profile.v1"
    assert routing_profile.get("role") == "execution"
    assert routing_profile.get("authoring_intent_required") is False
    assert routing_profile.get("prefer_existing_capability") is False

    exemplars, exemplar_source = resolve_workflow_discovery_exemplars(
        LAB_STATUS_DIGEST_WORKFLOW_ID
    )
    assert exemplar_source.startswith("text_relation:")
    assert "obligation watch" in (exemplars or {}).get("keywords", [])

    launch_contract, launch_source = resolve_workflow_launch_input_contract(
        LAB_STATUS_DIGEST_WORKFLOW_ID
    )
    assert launch_source.startswith("text_relation:")
    assert (launch_contract or {}).get("required_inputs") == ["prompt"]

    required_effects = (
        definition.metadata.get("required_effects_contract") or {}
    ).get("required_effects") or []
    assert [effect.get("effect_id") for effect in required_effects] == [
        "current_cross_source_status_evidence",
        "digest_persistence_and_readback",
    ]
    assert required_effects[0]["required_tools"] == [
        "jira_search",
        "repo_dossier_git_metadata",
        "fetch_concept_content",
    ]
    assert required_effects[1]["required_tools"] == [
        "upsert_singleton_text_relation",
        "fetch_concept_content",
        "get_text_relations_summary",
    ]

    def state_for(short_state_id: str) -> Any:
        return next(
            state
            for state_id, state in definition.states.items()
            if state_id.endswith(f"_{short_state_id}")
        )

    assert definition.initial_state.endswith("_read_prior_digest")
    expected_action_by_state = {
        "read_prior_digest": "workflow_mcp.invoke_tool",
        "read_current_jira": "workflow_mcp.invoke_tool",
        "read_current_repository": "workflow_mcp.invoke_tool",
        "synthesise_digest": "llm.action",
        "persist_digest": "workflow_mcp.invoke_tool",
        "read_back_digest": "workflow_mcp.invoke_tool",
        "read_back_text_relations": "workflow_mcp.invoke_tool",
    }
    for state_id, action_id in expected_action_by_state.items():
        assert state_for(state_id).actions[0].action_id == action_id

    synthesise_action = state_for("synthesise_digest").actions[0]
    assert synthesise_action.execution_mode == "llm"
    assert synthesise_action.llm_policy["tool_mode"] == "none"
    assert synthesise_action.llm_policy["selection_policy"] == "active_only"
    assert synthesise_action.llm_policy["max_output_tokens"] == 4096
    assert synthesise_action.validation_policy["output_format"] == "json_value"

    persist_state = state_for("persist_digest")
    persist_action = persist_state.actions[0]
    assert persist_action.inputs["tool_name"] == "upsert_singleton_text_relation"
    assert persist_action.inputs["concept_id"] == LAB_STATUS_DIGEST_WORK_PRODUCT_ID
    assert persist_action.inputs["policy"] == "replace_others"
    assert persist_state.metadata.get("mutation_authority") == {
        "schema_version": "workflow_step_mutation_authority.v1",
        "maximum_level": "additive_vontology",
        "reason_code": "actor_scoped_status_digest_work_product_update",
    }

    work_product = concept_service.get_concept_by_concept_id(
        LAB_STATUS_DIGEST_WORK_PRODUCT_ID
    )
    assert work_product is not None
    assert "#V#lab_status_digest" in (
        (work_product.get("relationships") or {}).get("is_an_instance_of") or []
    )


def test_lab_status_digest_prompt_pins_safety_and_readback(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_lab_status_digest_prompt_support()

    assert report.get("success") is True
    rows = get_texts_for_concept(
        LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in rows if (row or {}).get("text")), ""
    )
    normalised_prompt_text = " ".join(prompt_text.split())
    assert "not instruction authority" in normalised_prompt_text
    assert "Jira issue being Done" in normalised_prompt_text
    assert (
        "Deduplicate by source locator, owner, and due date"
        in normalised_prompt_text
    )
    assert "Do not close, reassign, transition, comment on" in normalised_prompt_text
    assert "policy `replace_others`" in normalised_prompt_text
    assert "canonical content and text-relation read-back" in normalised_prompt_text
    assert "Do not call tools or claim persistence" in normalised_prompt_text


def test_lab_status_digest_seed_uses_supported_singleton_policy() -> None:
    import json

    from src.backend.services import (
        lab_status_digest_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflow = next(
        item
        for item in payload["workflows"]
        if item["workflow_id"] == LAB_STATUS_DIGEST_WORKFLOW_ID
    )
    persist_step = next(
        step
        for step in workflow["publication_spec"]["steps"]
        if step["state_id"] == "persist_digest"
    )
    bindings = {
        item["key"]: item["value"] for item in persist_step["static_input_bindings"]
    }

    assert bindings["tool_name"] == "upsert_singleton_text_relation"
    assert bindings["policy"] == "replace_others"


def test_lab_status_digest_seed_bounds_cross_source_evidence() -> None:
    import json

    from src.backend.services import (
        lab_status_digest_workflow_vontology_service as mod,
    )

    payload = json.loads(mod._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    workflow = next(
        item
        for item in payload["workflows"]
        if item["workflow_id"] == LAB_STATUS_DIGEST_WORKFLOW_ID
    )
    steps = {
        step["state_id"]: step for step in workflow["publication_spec"]["steps"]
    }

    jira_bindings = {
        item["key"]: item["value"]
        for item in steps["read_current_jira"]["static_input_bindings"]
    }
    repo_bindings = {
        item["key"]: item["value"]
        for item in steps["read_current_repository"]["static_input_bindings"]
    }

    assert jira_bindings["max_results"] == 12
    assert repo_bindings["commit_limit"] == 12


def test_bootstrap_preserves_current_lab_status_digest_workflow(
    _reset_mock_db: Any,
) -> None:
    first = bootstrap_canonical_lab_status_digest_workflow()
    assert ((first.get("publication") or {}).get("counts") or {}).get(
        "workflows_published"
    ) == 1

    second = bootstrap_canonical_lab_status_digest_workflow()
    publication = second.get("publication") or {}
    assert publication.get("skipped") is True
    assert publication.get("skip_reason") == "existing_materialisation_valid"
    assert (publication.get("counts") or {}).get("workflows_published") == 0
