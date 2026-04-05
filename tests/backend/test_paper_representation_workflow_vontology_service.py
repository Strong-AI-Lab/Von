from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.arxiv_ingestion_testing_service import (
    cleanup_arxiv_paper_ingestion_test_artifacts,
    prepare_arxiv_paper_ingestion_test_fixture,
    verify_arxiv_paper_ingestion_test_result,
)
from src.backend.services.paper_representation_workflow_vontology_service import (
    ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_paper_representation_workflows,
    diff_canonical_paper_representation_workflow_repo_seed_bundle,
    export_canonical_paper_representation_workflow_repo_seed_bundle,
)
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.services.text_value_service import upsert_singleton_text_relation
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.durable import registry_factory
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_input_contract,
    resolve_workflow_publication_lifecycle,
    resolve_workflow_routing_profile,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PAPER_REPO_SEED_ASSET_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "paper_representation_workflow_seed_bundle.json"
)


def _upsert_workflow_json_text(
    *,
    workflow_id: str,
    predicate: str,
    payload: dict[str, Any],
) -> None:
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate=predicate,
        text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={"source": "test_paper_representation_workflow_vontology_service"},
        garbage_collect=True,
    )


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
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


def test_bootstrap_materialises_paper_representation_workflow_family(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_paper_representation_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 2
    assert counts.get("errors") == 0

    scholarly_definition = load_workflow_definition_from_vontology(
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert scholarly_definition is not None

    arxiv_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert arxiv_definition is not None
    arxiv_launch_contract = arxiv_definition.metadata.get("launch_input_contract")
    assert isinstance(arxiv_launch_contract, dict)
    assert arxiv_launch_contract.get("schema_version") == (
        "workflow_launch_input_contract.v1"
    )
    assert arxiv_launch_contract.get("required_inputs") == ["prompt"]
    resolved_launch_contract, resolved_launch_source = (
        resolve_workflow_launch_input_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert resolved_launch_contract == arxiv_launch_contract
    assert resolved_launch_source.startswith("text_relation:")
    input_mappings = arxiv_launch_contract.get("input_mappings")
    assert isinstance(input_mappings, list)
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "prompt"
        and item.get("source_expression") == "inputs.prompt"
        and item.get("required") is True
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "arxiv_id"
        and item.get("source_expression") == "inputs.arxiv_id"
        and item.get("required") is False
        for item in input_mappings
    )
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "source_uri"
        and item.get("source_expression") == "inputs.source_uri"
        and item.get("required") is False
        for item in input_mappings
    )
    routing_profile, routing_source = resolve_workflow_routing_profile(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(routing_profile, dict)
    assert routing_source.startswith("text_relation:")
    assert isinstance(routing_profile.get("role"), str)
    assert isinstance(routing_profile.get("authoring_intent_required"), bool)

    discovery_exemplars, discovery_source = resolve_workflow_discovery_exemplars(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(discovery_exemplars, dict)
    assert discovery_source.startswith("text_relation:")
    assert isinstance(discovery_exemplars.get("keywords"), list)
    assert discovery_exemplars.get("keywords")

    delegate_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="delegate_to_general_paper_workflow",
    )
    delegate_state = arxiv_definition.states[delegate_state_id]
    contract = delegate_state.metadata["subworkflow_contract"]
    assert contract["workflow_id"] == SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    assert "file_copy_concept_id" in contract["provided_inputs"]
    assert "paper_concept_id" in contract["provided_inputs"]
    assert "arxiv_id" in contract["provided_inputs"]
    assert "publication_date" in contract["provided_inputs"]
    assert "author_names" in contract["provided_inputs"]
    assert "topic_labels" in contract["provided_inputs"]
    delegate_action = delegate_state.actions[0]
    assert delegate_action.inputs.get("paper_concept_id") == {
        "$context_key": "paper_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_delegate_to_general_paper_workflow_paper_concept_id_to_paper_concept_id_parameter",
        "$required": False,
    }

    fetch_metadata_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="fetch_arxiv_metadata",
    )
    normalise_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="normalise_arxiv_source",
    )
    normalise_action = arxiv_definition.states[normalise_state_id].actions[0]
    assert normalise_action.inputs.get("arxiv_id") == {
        "$context_key": "arxiv_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_normalise_arxiv_source_arxiv_id_to_arxiv_id_parameter",
        "$required": False,
    }
    assert normalise_action.inputs.get("source_uri") == {
        "$context_key": "source_uri",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_normalise_arxiv_source_source_uri_to_source_uri_parameter",
        "$required": False,
    }
    assert normalise_action.inputs.get("prompt") == {
        "$context_key": "prompt",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_normalise_arxiv_source_prompt_to_prompt_parameter",
        "$required": True,
    }
    fetch_metadata_action = arxiv_definition.states[fetch_metadata_state_id].actions[0]
    assert fetch_metadata_action.action_id == "get_paper_metadata"
    assert fetch_metadata_action.inputs.get("arxiv_id") == {
        "$context_key": "arxiv_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_fetch_arxiv_metadata_arxiv_id_to_arxiv_id_parameter",
        "$required": True,
    }
    download_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="download_or_finalise",
    )
    download_action = arxiv_definition.states[download_state_id].actions[0]
    assert "materialise_scholarly_representation" not in download_action.inputs
    decide_acquisition_mode_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="decide_acquisition_mode",
    )
    finalise_cached_pdf_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="finalise_cached_pdf",
    )
    recover_partial_cache_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="recover_partial_cache_state",
    )
    delegate_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="delegate_to_general_paper_workflow",
    )
    decide_acquisition_mode_action = arxiv_definition.states[
        decide_acquisition_mode_state_id
    ].actions[0]
    assert decide_acquisition_mode_action.inputs.get("file_copy_concept_id") == {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_decide_acquisition_mode_file_copy_concept_id_to_file_copy_concept_id_parameter",
        "$required": False,
    }
    decide_transitions = {
        transition.reason: transition
        for transition in arxiv_definition.states[
            decide_acquisition_mode_state_id
        ].transitions
    }
    assert decide_transitions["existing_file_copy_available"].to_state == delegate_state_id
    assert decide_transitions["existing_file_copy_available"].condition_spec == {
        "key": "acquisition_mode",
        "kind": "context_value_equals",
        "value": "existing_file_copy",
    }
    assert decide_transitions["finalise_cached_pdf"].to_state == finalise_cached_pdf_state_id
    assert decide_transitions["recover_partial_cache"].to_state == (
        recover_partial_cache_state_id
    )
    assert decide_transitions["download_from_source"].to_state == download_state_id

    finalise_action = arxiv_definition.states[finalise_cached_pdf_state_id].actions[0]
    assert finalise_action.action_id == "finalise_cached_paper"
    assert finalise_action.inputs.get("arxiv_id") == {
        "$context_key": "arxiv_id",
        "$mapping_concept_id": "#V#workflow_mapping_arxiv_paper_representation_workflow_finalise_cached_pdf_arxiv_id_to_arxiv_id_parameter",
        "$required": True,
    }

    recover_action = arxiv_definition.states[recover_partial_cache_state_id].actions[0]
    assert recover_action.action_id == "workflow_control.context_set"
    assert recover_action.inputs.get("assignments") == [
        {"key": "cache_recovery_required", "value": True},
        {"key": "cache_recovery_action", "value": "reacquire_partial_cache"},
        {"key": "cache_recovery_reason", "value_from_context": "cache_state"},
        {
            "key": "cache_recovery_markdown_path",
            "value_from_context": "cached_markdown_path",
        },
    ]

    scholarly_verify_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="verify_representation",
    )
    scholarly_verify_action = scholarly_definition.states[scholarly_verify_state_id].actions[0]
    assert scholarly_verify_action.inputs.get("publication_date") == {
        "$context_key": "publication_date",
        "$mapping_concept_id": "#V#workflow_mapping_scholarly_paper_representation_workflow_verify_representation_publication_date_to_publication_date_parameter",
        "$required": True,
    }
    scholarly_normalise_state_id = authority_service._step_concept_id(
        workflow_id=SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="normalise_inputs",
    )
    scholarly_normalise_action = scholarly_definition.states[
        scholarly_normalise_state_id
    ].actions[0]
    assert scholarly_normalise_action.inputs.get("paper_concept_id") == {
        "$context_key": "paper_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_scholarly_paper_representation_workflow_normalise_inputs_paper_concept_id_to_paper_concept_id_parameter",
        "$required": False,
    }

    scholarly_concept = concept_service.get_concept_by_concept_id(
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert scholarly_concept is not None
    scholarly_types = (scholarly_concept.get("relationships") or {}).get(
        "is_an_instance_of"
    ) or []
    assert "#V#ai_workflow" in scholarly_types
    assert "#V#durable_workflow" in scholarly_types

    step_concept = concept_service.get_concept_by_concept_id(delegate_state_id)
    assert step_concept is not None
    step_types = (step_concept.get("relationships") or {}).get("is_an_instance_of") or []
    assert "#V#workflow_step" in step_types


def test_bootstrap_skips_republication_when_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_paper_representation_workflows()
    first_counts = (first_report.get("publication") or {}).get("counts") or {}
    assert first_counts.get("workflows_published") == 2

    second_report = bootstrap_canonical_paper_representation_workflows()
    second_authority = second_report.get("authority_contract") or {}
    second_preflight = second_report.get("materialisation_preflight") or {}
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_authority == {
        "canonical_source": "vontology",
        "repo_seed_role": "startup_seed_publication_and_repair_only",
        "request_path_dependency_allowed": False,
    }
    assert second_preflight.get("already_current") is True
    assert second_preflight.get("drift_detected") is False
    assert second_preflight.get("drift_workflow_ids") == []
    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_publication.get("materialisation_status") == "current"
    assert second_publication.get("drift_detected") is False
    assert second_publication.get("issue_codes") == []
    assert second_preflight.get("bundle_snapshot_drift_detected") is False
    assert second_preflight.get("bundle_snapshot_drift_workflow_ids") == []
    assert second_preflight.get("bundle_snapshot_issue_codes") == []
    assert second_publication.get("bundle_snapshot_drift_detected") is False
    assert second_publication.get("bundle_snapshot_drift_workflow_ids") == []
    assert second_publication.get("bundle_snapshot_issue_codes") == []
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0
    assert second_report.get("typed_workflow_ids") == []
    assert second_report.get("typed_step_ids") == []


def test_bootstrap_preserves_authoritative_state_when_repo_seed_snapshot_is_stale(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    mapping_concept_id = (
        "#V#workflow_mapping_arxiv_paper_representation_workflow_"
        "decide_acquisition_mode_file_copy_concept_id_to_file_copy_concept_id_parameter"
    )
    mapping_doc = concept_service.get_concept_by_concept_id(mapping_concept_id)
    assert mapping_doc is not None
    mapping_spec = dict(
        ((mapping_doc.get("concept_data") or {}).get("workflow_mapping_spec") or {})
    )
    mapping_spec["required"] = True
    concept_service.update_concept(
        mapping_concept_id,
        {"concept_data.workflow_mapping_spec": mapping_spec},
    )

    drifted_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert drifted_definition is not None
    drifted_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="decide_acquisition_mode",
    )
    drifted_action = drifted_definition.states[drifted_state_id].actions[0]
    assert drifted_action.inputs.get("file_copy_concept_id") == {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": mapping_concept_id,
        "$required": True,
    }

    launch_contract, _launch_source = resolve_workflow_launch_input_contract(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(launch_contract, dict)
    updated_launch_contract = json.loads(json.dumps(launch_contract))
    updated_launch_contract.setdefault("input_mappings", []).append(
        {
            "target_context_key": "paper_concept_id",
            "source_expression": "inputs.paper_concept_id",
            "extractor": "identity",
            "required": False,
            "description": (
                "Allow callers to pin an existing paper concept when the "
                "authoritative Vontology workflow is updated."
            ),
        }
    )
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLaunchInputContractJson",
        payload=updated_launch_contract,
    )
    updated_routing_profile = {
        "schema_version": "workflow_routing_profile.v1",
        "role": "authoring",
        "authoring_intent_required": True,
        "prefer_existing_capability": True,
    }
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRoutingProfileJson",
        payload=updated_routing_profile,
    )
    updated_discovery_exemplars = {
        "schema_version": "workflow_discovery_exemplars.v1",
        "keywords": ["arxiv", "paper", "authority-only-export-sentinel"],
        "examples": ["Represent this arXiv paper from its identifier."],
    }
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowDiscoveryExemplarsJson",
        payload=updated_discovery_exemplars,
    )

    repair_report = bootstrap_canonical_paper_representation_workflows()
    repair_preflight = repair_report.get("materialisation_preflight") or {}
    repair_publication = repair_report.get("publication") or {}
    repair_counts = repair_publication.get("counts") or {}

    assert repair_preflight.get("already_current") is True
    assert repair_preflight.get("drift_detected") is False
    assert repair_preflight.get("drift_workflow_ids") == []
    assert repair_preflight.get("issue_codes") == []
    assert repair_preflight.get("bundle_snapshot_drift_detected") is False
    assert repair_preflight.get("bundle_snapshot_drift_workflow_ids") == []
    assert repair_preflight.get("bundle_snapshot_issue_codes") == []
    assert repair_publication.get("skipped") is True
    assert repair_publication.get("materialisation_status") == "current"
    assert repair_publication.get("drift_detected") is False
    assert repair_publication.get("issue_codes") == []
    assert repair_publication.get("bundle_snapshot_drift_detected") is False
    assert repair_publication.get("bundle_snapshot_drift_workflow_ids") == []
    assert repair_publication.get("bundle_snapshot_issue_codes") == []
    assert repair_counts.get("workflows_published") == 0
    assert repair_counts.get("errors") == 0

    repaired_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert repaired_definition is not None
    repaired_action = repaired_definition.states[drifted_state_id].actions[0]
    assert repaired_action.inputs.get("file_copy_concept_id") == {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": mapping_concept_id,
        "$required": True,
    }
    repaired_launch_contract, repaired_launch_source = (
        resolve_workflow_launch_input_contract(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert repaired_launch_source.startswith("text_relation:")
    assert repaired_launch_contract == updated_launch_contract
    repaired_routing_profile, repaired_routing_source = resolve_workflow_routing_profile(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert repaired_routing_source.startswith("text_relation:")
    assert repaired_routing_profile == updated_routing_profile
    repaired_discovery_exemplars, repaired_discovery_source = (
        resolve_workflow_discovery_exemplars(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    )
    assert repaired_discovery_source.startswith("text_relation:")
    assert repaired_discovery_exemplars == updated_discovery_exemplars


def test_bootstrap_repairs_explicit_unpublished_lifecycle(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    unpublished_payload = {
        "schema_version": "workflow_publication_lifecycle.v1",
        "phase": "draft",
        "published": False,
        "validation_passed": False,
        "postconditions_verified": False,
    }
    concept_service.update_concept(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        {"concept_data.workflow_publication_lifecycle": unpublished_payload},
    )
    upsert_singleton_text_relation(
        subject_concept_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLifecycleJson",
        text=json.dumps(unpublished_payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={"source": "test_bootstrap_repairs_explicit_unpublished_lifecycle"},
        garbage_collect=True,
    )
    invalidate_workflow_discovery_executability_caches()

    repair_report = bootstrap_canonical_paper_representation_workflows()
    preflight = repair_report.get("materialisation_preflight") or {}
    publication = repair_report.get("publication") or {}

    assert preflight.get("drift_detected") is True
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        preflight.get("drift_workflow_ids") or []
    )
    assert "workflow_not_published" in (preflight.get("issue_codes") or [])
    assert publication.get("materialisation_status") == "repaired_from_repo_seed"
    assert ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID in (
        publication.get("published_workflow_ids") or []
    )

    repaired_lifecycle, repaired_source = resolve_workflow_publication_lifecycle(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert repaired_lifecycle == {
        "schema_version": "workflow_publication_lifecycle.v1",
        "phase": "published",
        "published": True,
    }
    assert repaired_source in {
        "concept_data",
        "text_relation:#V#hasWorkflowLifecycleJson",
    }


def test_export_refreshes_paper_repo_seed_bundle_from_authority(
    _reset_mock_db: Any,
    tmp_path: Path,
) -> None:
    bootstrap_canonical_paper_representation_workflows()

    updated_launch_contract = {
        "schema_version": "workflow_launch_input_contract.v1",
        "required_inputs": ["prompt"],
        "input_mappings": [
            {
                "target_context_key": "prompt",
                "source_expression": "inputs.prompt",
                "extractor": "identity",
                "required": True,
                "description": "Pass through the prompt.",
            },
            {
                "target_context_key": "paper_concept_id",
                "source_expression": "inputs.paper_concept_id",
                "extractor": "identity",
                "required": False,
                "description": "Allow an explicit paper concept override.",
            },
        ],
    }
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowLaunchInputContractJson",
        payload=updated_launch_contract,
    )
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowRoutingProfileJson",
        payload={
            "schema_version": "workflow_routing_profile.v1",
            "role": "authoring",
            "authoring_intent_required": True,
            "prefer_existing_capability": True,
        },
    )
    _upsert_workflow_json_text(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        predicate="#V#hasWorkflowDiscoveryExemplarsJson",
        payload={
            "schema_version": "workflow_discovery_exemplars.v1",
            "keywords": ["arxiv", "paper", "authority-export"],
            "examples": ["Refresh the repo seed from authoritative paper workflow state."],
        },
    )

    tmp_asset_path = tmp_path / _PAPER_REPO_SEED_ASSET_PATH.name
    tmp_asset_path.write_text(
        _PAPER_REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    before_diff = diff_canonical_paper_representation_workflow_repo_seed_bundle(
        asset_path=tmp_asset_path
    )
    assert before_diff.get("has_differences") is True

    export_report = export_canonical_paper_representation_workflow_repo_seed_bundle(
        asset_path=tmp_asset_path
    )
    assert export_report.get("asset_path") == str(tmp_asset_path.resolve())
    assert export_report.get("workflow_ids") == [
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    ]

    payload = json.loads(tmp_asset_path.read_text(encoding="utf-8"))
    workflows = payload.get("workflows") or []
    arxiv_entry = next(
        item
        for item in workflows
        if item.get("workflow_id") == ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert arxiv_entry.get("launch_input_contract") == updated_launch_contract
    text_relations = arxiv_entry.get("text_relations") or []
    assert any(
        item.get("predicate") == "#V#hasWorkflowRoutingProfileJson"
        for item in text_relations
    )
    assert any(
        item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
        for item in text_relations
    )

    after_diff = diff_canonical_paper_representation_workflow_repo_seed_bundle(
        asset_path=tmp_asset_path
    )
    assert after_diff.get("has_differences") is False


def test_live_arxiv_paper_representation_workflow_acceptance(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.getenv("VON_RUN_LIVE_ARXIV_WORKFLOW_ACCEPTANCE") != "1":
        pytest.skip("Set VON_RUN_LIVE_ARXIV_WORKFLOW_ACCEPTANCE=1 to run live arXiv acceptance.")

    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "local")
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "0")

    bootstrap_canonical_paper_representation_workflows()
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    fixture = prepare_arxiv_paper_ingestion_test_fixture(
        prompt_text="Represent this paper https://arxiv.org/abs/2603.01896",
        source_uri="https://arxiv.org/abs/2603.01896",
        arxiv_id="2603.01896",
        user_concept_id="#V#michael_witbrock",
        timeout_seconds=45.0,
        repair_existing_artifacts=True,
    )
    assert fixture.get("success") is True, fixture

    result = WorkflowExecutor(
        registry=registry_factory.build_durable_action_registry(),
        max_transitions=40,
    ).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
            user_concept_id="#V#michael_witbrock",
            org_concept_id="#V#university_of_auckland_strong_ai_lab",
        ),
        data={
            "prompt": fixture["prompt_text"],
            "arxiv_id": fixture["arxiv_id"],
            "source_uri": fixture["source_uri"],
            "original_filename": fixture["original_filename"],
        },
    )

    verification = verify_arxiv_paper_ingestion_test_result(
        workflow_execution={
            "completed": result.completed,
            "final_state": result.final_state,
            "error": result.error,
            "final_status": "completed" if result.completed else "failed",
            "outputs": dict(result.data),
        },
        arxiv_id=fixture["arxiv_id"],
        source_uri=fixture["source_uri"],
        expected_title=fixture["expected_title"],
        expected_summary=fixture["expected_summary"],
        expected_publication_date=fixture["expected_publication_date"],
        expected_author_names=fixture["expected_author_names"],
        expected_author_concept_ids=fixture["expected_author_concept_ids"],
        expected_topic_labels=fixture["expected_topic_labels"],
        expected_topic_concept_ids=fixture["expected_topic_concept_ids"],
        paper_concept_id=fixture["paper_concept_id"],
    )

    cleanup = cleanup_arxiv_paper_ingestion_test_artifacts(
        paper_concept_id=verification.get("paper_concept_id") or fixture["paper_concept_id"],
        file_copy_concept_id=verification.get("file_copy_concept_id"),
        author_concept_ids=fixture["expected_author_concept_ids"],
        topic_concept_ids=fixture["expected_topic_concept_ids"],
        preexisting_author_concept_ids=fixture["preexisting_author_concept_ids"],
        preexisting_topic_concept_ids=fixture["preexisting_topic_concept_ids"],
    )

    assert result.completed is True
    assert result.final_state == authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="completed",
    )
    assert verification.get("verification_passed") is True, verification
    assert cleanup.get("cleanup_passed") is True, cleanup
