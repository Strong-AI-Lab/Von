"""Tests for governed model-registry release inputs and read-back."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.backend.services import model_registry_release_vontology_service as mod


def _bundle():
    return mod.load_model_registry_release_bundle()


def _matching_snapshot(bundle):
    expected = bundle["expected_read_back"]
    pricing = next(
        item["json"]
        for item in bundle["text_relations"]
        if item["resolved_predicate"] == "#V#has_model_pricing_json"
    )
    capabilities = next(
        item["json"]
        for item in bundle["text_relations"]
        if item["resolved_predicate"] == "#V#has_model_capabilities_json"
    )
    return {
        "source": "vontology_graph",
        "registry_concept_id": "#V#default_model_registry",
        "models": [
            {
                "provider": expected["provider"],
                "model_id": expected["model_id"],
                "registry_entry_id": expected["registry_entry_id"],
                "concept_id": expected["model_concept_id"],
                "pricing": copy.deepcopy(pricing),
                "capabilities": copy.deepcopy(capabilities),
                "api_profiles": [
                    {
                        "profile_concept_id": expected["api_profile_id"],
                        "api_surface": expected["api_surface"],
                        "structured_tool_calling": expected["structured_tool_calling"],
                        "tool_continuation_mode": expected["tool_continuation_mode"],
                        "response_storage_policy": expected["response_storage_policy"],
                        "connection_id": expected["connection_id"],
                    }
                ],
            }
        ],
    }


def test_release_asset_is_tracked_friendly_and_declares_exact_stateless_profile():
    bundle = _bundle()

    assert Path(bundle["asset_path"]).suffix == ".in"
    assert bundle["schema_version"] == "model_registry_release_bundle.v1"
    assert bundle["release_version"] == "1"
    assert bundle["authority_role"] == "repo_release_input_only"
    expected = bundle["expected_read_back"]
    assert expected == {
        "provider": "gemini",
        "model_id": "gemini-3.7-flash",
        "registry_entry_id": "#V#gemini_3_7_flash_registry_entry",
        "model_concept_id": "#V#google_gemini_3_7_flash",
        "api_profile_id": "#V#gemini_3_7_flash_interactions_profile",
        "api_surface": "interactions",
        "structured_tool_calling": "supported",
        "tool_continuation_mode": "stateless",
        "response_storage_policy": "disabled",
        "connection_id": "gemini_developer_api",
        "pricing_version": "gemini-standard-introductory-2026-08-13",
        "capabilities_version": "gemini-3.7-flash-ga-2026-08-13",
    }

    capabilities_relation = next(
        item
        for item in bundle["text_relations"]
        if item["resolved_predicate"] == "#V#has_model_capabilities_json"
    )
    capabilities = json.loads(capabilities_relation["resolved_text"])
    assert capabilities["input_token_limit"] == 1_048_576
    assert capabilities["output_token_limit"] == 65_536
    assert capabilities["generation_parameter_policy"]["omit"] == [
        "temperature",
        "top_p",
        "top_k",
        "candidate_count",
        "thinking_budget",
    ]
    pricing_relation = next(
        item
        for item in bundle["text_relations"]
        if item["resolved_predicate"] == "#V#has_model_pricing_json"
    )
    pricing = json.loads(pricing_relation["resolved_text"])
    assert pricing["unit_tokens"] == 1_000_000
    assert pricing["rates"] == {"input_tokens": 0.75, "output_tokens": 3.75}
    assert pricing["effective_until_utc"] == "2027-01-01T00:00:00Z"
    assert pricing["applicability"] == {
        "effective_service_tiers": ["standard"],
        "connection_ids": ["gemini_developer_api"],
    }


def test_release_dependency_closure_covers_every_concept_and_predicate():
    bundle = _bundle()
    required = set(bundle["required_existing_concept_ids"])
    created = {item["concept_id"] for item in bundle["concepts"]}
    known = required | created

    assert "#V#hasName" in required
    assert "#V#has_model_capabilities_json" in created
    assert "#V#has_provider_connection_id" in created
    for concept in bundle["concepts"]:
        parent_id = (
            "#V#predicate"
            if concept["kind"] == "predicate"
            else concept["instance_of_type"]
        )
        assert parent_id in known
    for relationship in bundle["relationships"]:
        assert {
            relationship["source_id"],
            relationship["predicate"],
            relationship["target_id"],
        } <= known
    for relation in bundle["text_relations"]:
        assert relation["subject_concept_id"] in known
        assert relation["resolved_predicate"] in known


def test_validator_rejects_mismatched_model_identity_and_non_positive_rates():
    mismatched = copy.deepcopy(_bundle())
    mismatched["model"]["concept_id"] = "#V#wrong_model"
    with pytest.raises(mod.ModelRegistryReleaseError) as identity_error:
        mod.validate_model_registry_release_bundle(mismatched)
    assert identity_error.value.code == (
        "model_registry_release_read_back_contract_invalid"
    )

    invalid_pricing = copy.deepcopy(_bundle())
    pricing = next(
        item["json"]
        for item in invalid_pricing["text_relations"]
        if item["resolved_predicate"] == "#V#has_model_pricing_json"
    )
    pricing["rates"]["input_tokens"] = 0
    with pytest.raises(mod.ModelRegistryReleaseError) as pricing_error:
        mod.validate_model_registry_release_bundle(invalid_pricing)
    assert pricing_error.value.code == "model_registry_release_pricing_contract_invalid"

    invalid_capabilities = copy.deepcopy(_bundle())
    capabilities = next(
        item["json"]
        for item in invalid_capabilities["text_relations"]
        if item["resolved_predicate"] == "#V#has_model_capabilities_json"
    )
    capabilities["schema_version"] = "llm_model_capabilities.v0"
    with pytest.raises(mod.ModelRegistryReleaseError) as capabilities_error:
        mod.validate_model_registry_release_bundle(invalid_capabilities)
    assert capabilities_error.value.code == (
        "model_registry_release_capabilities_contract_invalid"
    )


def test_validator_requires_exact_standard_pricing_scope_and_valid_window():
    invalid_scope = copy.deepcopy(_bundle())
    pricing = next(
        item["json"]
        for item in invalid_scope["text_relations"]
        if item["resolved_predicate"] == "#V#has_model_pricing_json"
    )
    pricing["applicability"]["effective_service_tiers"] = ["priority"]
    with pytest.raises(mod.ModelRegistryReleaseError) as scope_error:
        mod.validate_model_registry_release_bundle(invalid_scope)
    assert scope_error.value.code == "model_registry_release_pricing_contract_invalid"

    invalid_window = copy.deepcopy(_bundle())
    pricing = next(
        item["json"]
        for item in invalid_window["text_relations"]
        if item["resolved_predicate"] == "#V#has_model_pricing_json"
    )
    pricing["effective_until_utc"] = pricing["effective_at_utc"]
    with pytest.raises(mod.ModelRegistryReleaseError) as window_error:
        mod.validate_model_registry_release_bundle(invalid_window)
    assert window_error.value.code == "model_registry_release_pricing_contract_invalid"


def test_release_plan_uses_only_exact_governed_operations():
    bundle = _bundle()
    assert bundle["relationships"][-1] == {
        "source_id": "#V#default_model_registry",
        "predicate": "#V#has_model_entry",
        "target_id": "#V#gemini_3_7_flash_registry_entry",
    }
    plan = mod.build_model_registry_release_plan(
        bundle, concept_exists=lambda _concept_id: False
    )
    retry_plan = mod.build_model_registry_release_plan(
        bundle,
        concept_exists=lambda concept_id: (
            concept_id == "#V#has_model_capabilities_json"
        ),
    )

    assert plan["authority_requirement"] == (
        "authenticated_global_ontology_publication"
    )
    assert len(plan["operations"]) == 19
    assert len({item["operation_id"] for item in plan["operations"]}) == 19
    retry_ids = {item["operation_id"] for item in retry_plan["operations"]}
    assert retry_ids < {item["operation_id"] for item in plan["operations"]}
    assert {item["method_name"] for item in plan["operations"]} == {
        "create_concepts",
        "add_relationship",
        "upsert_text_relation",
        "upsert_singleton_text_relation",
    }
    for operation in plan["operations"]:
        arguments = operation["arguments"]
        assert "created_by_concept_id" not in arguments
        assert "organisation_concept_id" not in arguments
        assert "user_id" not in arguments
        if operation["method_name"] == "create_concepts":
            assert arguments["scope_mode"] == "global_general"
            assert arguments["duplicate_resolution_mode"] == "canonical_id_only"

    profile_storage = next(
        operation
        for operation in plan["operations"]
        if operation["method_name"] == "upsert_singleton_text_relation"
        and operation["arguments"]["predicate"] == "#V#has_response_storage_policy"
    )
    assert profile_storage["arguments"]["concept_id"] == (
        "#V#gemini_3_7_flash_interactions_profile"
    )
    assert profile_storage["arguments"]["text"] == "disabled"
    assert plan["operations"][-1]["method_name"] == "add_relationship"
    assert plan["operations"][-1]["arguments"] == {
        "source_id": "#V#default_model_registry",
        "predicate": "#V#has_model_entry",
        "target": "#V#gemini_3_7_flash_registry_entry",
    }


def test_canonical_read_back_matches_exact_registry_projection():
    bundle = _bundle()

    report = mod.verify_model_registry_release_read_back(
        bundle, _matching_snapshot(bundle)
    )

    assert report["success"] is True
    assert report["matched"] is True
    assert report["mismatches"] == {}
    assert report["actual"]["api_surface"] == "interactions"
    assert report["actual"]["connection_id"] == "gemini_developer_api"


@pytest.mark.parametrize("payload_name", ["pricing", "capabilities"])
def test_canonical_read_back_rejects_payload_changes_with_unchanged_version(
    payload_name,
):
    bundle = _bundle()
    snapshot = _matching_snapshot(bundle)
    entry = snapshot["models"][0]
    if payload_name == "pricing":
        entry["pricing"]["rates"]["input_tokens"] = 999
    else:
        entry["capabilities"]["input_token_limit"] = 1

    report = mod.verify_model_registry_release_read_back(bundle, snapshot)

    assert report["success"] is False
    assert report["matched"] is False
    assert f"{payload_name}_payload" in report["mismatches"]


def test_publish_is_idempotent_when_canonical_read_back_already_matches():
    bundle = _bundle()
    invocations = []

    result = mod.publish_model_registry_release_bundle(
        invoke_governed_method=lambda method, arguments: invocations.append(
            (method, arguments)
        ),
        concept_exists=lambda _concept_id: True,
        snapshot_reader=lambda: _matching_snapshot(bundle),
        refreshed_snapshot_reader=lambda: pytest.fail(
            "an idempotent read-back must not refresh after a write"
        ),
    )

    assert result["success"] is True
    assert result["changed"] is False
    assert result["idempotent"] is True
    assert result["operation_results"] == []
    assert invocations == []


def test_publish_fails_before_effect_when_a_required_dependency_is_missing():
    invocations = []

    with pytest.raises(mod.ModelRegistryReleaseError) as exc_info:
        mod.publish_model_registry_release_bundle(
            invoke_governed_method=lambda method, arguments: invocations.append(
                (method, arguments)
            ),
            concept_exists=lambda concept_id: concept_id != "#V#gemini_provider",
            snapshot_reader=lambda: pytest.fail(
                "dependency failure must precede registry hydration"
            ),
        )

    assert exc_info.value.code == "model_registry_release_dependency_missing"
    assert exc_info.value.details["missing_required_concept_ids"] == [
        "#V#gemini_provider"
    ]
    assert invocations == []


def test_publish_invokes_governed_plan_and_requires_refreshed_read_back():
    bundle = _bundle()
    required = set(bundle["required_existing_concept_ids"])
    invocations = []

    def invoke(method, arguments):
        invocations.append((method, arguments))
        if method == "create_concepts":
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "total": 1,
                "successful": 1,
            }
        if method == "add_relationship":
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "added": True,
            }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "relation_created": True,
        }

    result = mod.publish_model_registry_release_bundle(
        invoke_governed_method=invoke,
        concept_exists=lambda concept_id: concept_id in required,
        snapshot_reader=lambda: {"source": "vontology_graph", "models": []},
        refreshed_snapshot_reader=lambda: _matching_snapshot(bundle),
    )

    assert result["success"] is True
    assert result["changed"] is True
    assert result["canonical_read_back"]["matched"] is True
    assert len(invocations) == 19
    assert len(result["operation_results"]) == 19


def test_publish_failure_before_final_activation_leaves_entry_dormant():
    bundle = _bundle()
    required = set(bundle["required_existing_concept_ids"])
    invocations = []

    def invoke(method, arguments):
        invocations.append((method, arguments))
        if method == "create_concepts":
            return {"success": True, "total": 1, "successful": 1}
        if method == "upsert_singleton_text_relation":
            return {"success": False, "effect_status": "failed"}
        return {"success": True}

    with pytest.raises(mod.ModelRegistryReleaseError) as exc_info:
        mod.publish_model_registry_release_bundle(
            invoke_governed_method=invoke,
            concept_exists=lambda concept_id: concept_id in required,
            snapshot_reader=lambda: {"source": "vontology_graph", "models": []},
        )

    assert exc_info.value.code == "model_registry_release_governed_operation_failed"
    assert not any(
        method == "add_relationship"
        and arguments
        == {
            "source_id": "#V#default_model_registry",
            "predicate": "#V#has_model_entry",
            "target": "#V#gemini_3_7_flash_registry_entry",
        }
        for method, arguments in invocations
    )


def test_publish_fails_closed_when_canonical_read_back_does_not_match():
    bundle = _bundle()
    required = set(bundle["required_existing_concept_ids"])

    def invoke(method, _arguments):
        if method == "create_concepts":
            return {"success": True, "total": 1, "successful": 1}
        return {"success": True}

    with pytest.raises(mod.ModelRegistryReleaseError) as exc_info:
        mod.publish_model_registry_release_bundle(
            invoke_governed_method=invoke,
            concept_exists=lambda concept_id: concept_id in required,
            snapshot_reader=lambda: {"source": "vontology_graph", "models": []},
            refreshed_snapshot_reader=lambda: {
                "source": "vontology_graph",
                "models": [],
            },
        )

    assert exc_info.value.code == ("model_registry_release_canonical_read_back_failed")
    assert exc_info.value.details["canonical_read_back"]["matched"] is False
